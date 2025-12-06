import sqlite3
import time
import calendar
from datetime import datetime, date, timedelta
import pytz
from flask import Flask, render_template, request, g, redirect, url_for, flash, session, jsonify
import re
import math
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from validate_docbr import CPF, CNPJ


# Tenta importar webview, mas não quebra se não estiver instalado
try:
    import webview
except ImportError:
    webview = None


# --- CONFIGURAÇÃO ---
app = Flask(__name__)
app.config['SECRET_KEY'] = 'segredo_v10_platinum_final_production'
DATABASE = 'estacionamento.db'
BR_TZ = pytz.timezone('America/Sao_Paulo')
MAX_SQL_DATE = '2100-01-01'
PASSADO_DISTANTE_SQL = '1900-01-01'


import logging

# Configuração simples de log em arquivo
logging.basicConfig(
    filename='erros_sistema.log',
    level=logging.ERROR,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

def registrar_log(acao, detalhes):
    """
    Grava um log de auditoria no banco de dados.
    """
    try:
        db = get_db()
        usuario_id = session.get('user_id')
        usuario_nome = session.get('user_nome', 'Sistema/Anônimo')
        ip = request.remote_addr
        
        detalhes_completo = f"[{usuario_nome}] {detalhes}"
        
        # Verifica se a tabela LOGS existe antes de tentar gravar (para evitar erros em bancos antigos)
        try:
            db.execute(
                "INSERT INTO LOGS (usuario_id, acao, detalhes, ip) VALUES (?, ?, ?, ?)",
                (usuario_id, acao, detalhes_completo, ip)
            ).connection.commit()
        except sqlite3.OperationalError:
            # Se a tabela não existir, apenas ignora (ou cria log no arquivo)
            logging.error(f"Tabela LOGS não encontrada ao tentar registrar: {acao}")
            
    except Exception as e:
        print(f"Erro ao gravar log: {e}")


# --- HELPERS ---
def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)    
        db.row_factory = sqlite3.Row
    return db


@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None: db.close()


def obter_hora_br(): return datetime.now(BR_TZ).replace(tzinfo=None)
def obter_data_br(): return obter_hora_br().date()


def safe_float(valor):
    if not valor: return 0.0
    try: return float(str(valor).replace(',', '.'))
    except ValueError: return 0.0


def add_months(source_date, months):
    month = source_date.month - 1 + months
    year = source_date.year + month // 12
    month = month % 12 + 1
    day = min(source_date.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


# Helper para Relatórios: Converte HHMM em HH:MM
def parse_time_input(time_str):
    if not time_str: return None
    clean = time_str.replace(':', '').strip()
    if len(clean) == 4 and clean.isdigit():
        h, m = clean[:2], clean[2:]
        if int(h) > 23 or int(m) > 59: return "00:00"
        return f"{h}:{m}"
    return time_str


# Helper de Segurança: Busca tipo histórico confiável
def get_historical_type(db, placa):
    # 1. Checa cadastro
    cli = db.execute("SELECT tipo_veiculo FROM CLIENTES WHERE placa = ?", (placa,)).fetchone()
    if cli and cli['tipo_veiculo']: return cli['tipo_veiculo']
    # 2. Checa histórico de tickets pagos
    tkt = db.execute("SELECT tipo FROM TICKETS WHERE placa = ? AND status = 'PAGO' ORDER BY hora_saida DESC LIMIT 1", (placa,)).fetchone()
    if tkt and tkt['tipo'] in ['CARRO', 'MOTO']: return tkt['tipo']
    return None


# NOVO HELPER: Validação de Formato CPF/CNPJ
def validar_cpf_cnpj(documento):
    if not documento:
        return True, "Opcional."
    
    doc_limpo = re.sub(r'[^0-9]', '', documento)
    
    if len(doc_limpo) == 11:
        cpf_validador = CPF()
        if cpf_validador.validate(doc_limpo):
            return True, "CPF válido."
        else:
            return False, "CPF inválido."
            
    elif len(doc_limpo) == 14:
        cnpj_validador = CNPJ()
        if cnpj_validador.validate(doc_limpo):
            return True, "CNPJ válido."
        else:
            return False, "CNPJ inválido."
            
    # Se o usuário digitou algo, mas não 11 ou 14 dígitos (e o campo é opcional)
    return False, "Documento deve ter 11 (CPF) ou 14 (CNPJ) dígitos." if len(doc_limpo) not in [0, 11, 14] else (True, "Opcional")


# --- INJEÇÃO DE CONTEXTO ---
@app.context_processor
def inject_globals():
    db = get_db()
    estab = None
    try: estab = db.execute("SELECT * FROM ESTABELECIMENTO WHERE id=1").fetchone()
    except: pass
    if not estab: estab = {'nome': 'ParkSystem', 'cnpj':'', 'endereco':'', 'telefone':'', 'total_vagas': 50}
    caixa_aberto = None
    try: caixa_aberto = db.execute("SELECT id, saldo_inicial FROM CAIXA WHERE status = 'ABERTO'").fetchone()
    except: pass
    faturamento = 0.0
    status_caixa = 'FECHADO'
    if caixa_aberto:
        status_caixa = 'ABERTO'
        try:
            vendas = db.execute("SELECT SUM(valor_total) FROM TICKETS WHERE status = 'PAGO' AND caixa_id = ?", (caixa_aberto['id'],)).fetchone()[0]
            faturamento = (vendas or 0.0)
        except: pass


    return dict(obter_data_br=obter_data_br, obter_hora_br=obter_hora_br, estab=estab, caixa_status=status_caixa, faturamento=faturamento)


# --- FORMATADORES ---
def format_datetime(value, format='%d/%m/%Y %H:%M'):
    if value is None: return ""
    if isinstance(value, str):
        try:
            try: dt_obj = datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
            except ValueError: dt_obj = datetime.strptime(value, '%Y-%m-%d %H:%M')
            return dt_obj.strftime(format)
        except ValueError:
            try:
                dt_obj = datetime.strptime(value, '%Y-%m-%d')
                if '%H:%M' in format: return dt_obj.strftime('%d/%m/%Y')
                return dt_obj.strftime(format)
            except: return value
    if isinstance(value, datetime): return value.strftime(format)
    if isinstance(value, date): return value.strftime(format)
    return value
app.jinja_env.filters['format_datetime'] = format_datetime


def fmt_placa(p): return f"{p[:3]}-{p[3:]}" if p and len(p)==7 else p
app.jinja_env.filters['fmt_placa'] = fmt_placa
def fmt_data(d): return format_datetime(d, format='%d/%m/%Y %H:%M:%S')


@app.template_filter('to_datetime')
def to_datetime_filter(value):
    from datetime import datetime
    
    if not value: return datetime.min
    if isinstance(value, datetime): return value
    
    # 1. Tenta o formato completo com segundos
    try:
        return datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
    except ValueError:
        pass
        
    # 2. Tenta o formato sem segundos
    try:
        return datetime.strptime(value, '%Y-%m-%d %H:%M')
    except ValueError:
        pass
        
    # 3. Tenta o formato apenas de data (Mais provável para data_fim_ciclo)
    try:
        return datetime.strptime(value, '%Y-%m-%d')
    except ValueError:
        pass
        
    # Retorna o valor mínimo (data antiga) se tudo falhar
    return datetime.min


def gerar_codigo_visual(seq, cid, tipo):
    if tipo == 'MENSALISTA': return f"MEN-{str(cid or 0).zfill(4)}"
    return f"TCK-{str(seq or 0).zfill(4)}"


# --- DECORADORES ---
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session: return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function


def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if session.get('user_perfil') != 'ADMIN': flash('Acesso restrito.', 'danger'); return redirect(url_for('home'))
        return f(*args, **kwargs)
    return decorated_function


def caixa_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        db = get_db()
        caixa = db.execute("SELECT * FROM CAIXA WHERE status = 'ABERTO' ORDER BY id DESC LIMIT 1").fetchone()
        if not caixa: flash('Abra o caixa para realizar esta operação.', 'warning'); return redirect(url_for('abrir_caixa'))
        try: dt_abertura = datetime.strptime(caixa['data_abertura'], '%Y-%m-%d %H:%M:%S').date()
        except: dt_abertura = obter_data_br()
        if dt_abertura < obter_data_br():
            tot = db.execute("SELECT SUM(valor_total) FROM TICKETS WHERE status = 'PAGO' AND caixa_id = ?", (caixa['id'],)).fetchone()[0] or 0.0
            agora = obter_hora_br().strftime('%Y-%m-%d %H:%M:%S')
            db.execute("UPDATE CAIXA SET data_fechamento=?, saldo_final=?, status='FECHADO_AUTO' WHERE id=?", (agora, caixa['saldo_inicial']+tot, caixa['id'])).connection.commit()
            
            if tot > 0:
                obs = f"Fechamento Auto (Virada de Dia) Caixa #{caixa['id']}"
                db.execute("INSERT INTO RECEITAS (descricao, valor, data_vencimento, data_recebimento, categoria, recorrente, observacao, status) VALUES (?,?,?,?,?,?,?,?)",
                            (f"Fechamento Auto Caixa #{caixa['id']}", tot, obter_data_br().strftime('%Y-%m-%d'), obter_data_br().strftime('%Y-%m-%d'), 'Fechamento de Caixa', 0, obs, 'RECEBIDO')).connection.commit()
            
            db.execute("INSERT INTO CAIXA (data_abertura, saldo_inicial, usuario_abertura_id, status) VALUES (?, ?, ?, ?)", (agora, caixa['saldo_inicial'], session['user_id'], 'ABERTO')).connection.commit()
            flash('Novo caixa aberto (Virada de dia).', 'warning')
        return f(*args, **kwargs)
    return decorated_function


# --- BANCO DE DADOS ---
def init_db():
    with app.app_context():
        db = get_db()
        db.execute("CREATE TABLE IF NOT EXISTS TICKETS (id INTEGER PRIMARY KEY, placa TEXT NOT NULL, tipo TEXT DEFAULT 'CARRO', local_vaga TEXT, numero_sequencial INTEGER, hora_entrada TEXT NOT NULL, hora_saida TEXT, valor_total REAL, status TEXT DEFAULT 'ESTACIONADO', caixa_id INTEGER, forma_pagamento TEXT DEFAULT 'DINHEIRO')").connection.commit()
        # CLIEENTES: Placa UNIQUE, mas os outros campos não
        db.execute("CREATE TABLE IF NOT EXISTS CLIENTES (id INTEGER PRIMARY KEY AUTOINCREMENT, nome TEXT NOT NULL, telefone TEXT, placa TEXT NOT NULL UNIQUE)").connection.commit()
        db.execute("CREATE TABLE IF NOT EXISTS ESTABELECIMENTO (id INTEGER PRIMARY KEY, nome TEXT, cnpj TEXT, endereco TEXT, telefone TEXT, total_vagas INTEGER DEFAULT 50)").connection.commit()
        db.execute("CREATE TABLE IF NOT EXISTS TARIFAS (id INTEGER PRIMARY KEY, valor_carro REAL, valor_moto REAL, teto_diaria REAL, tolerancia_minutos INTEGER)").connection.commit()
        db.execute("CREATE TABLE IF NOT EXISTS USUARIOS (id INTEGER PRIMARY KEY AUTOINCREMENT, nome TEXT NOT NULL, username TEXT NOT NULL UNIQUE, senha TEXT NOT NULL, perfil TEXT NOT NULL)").connection.commit()
        db.execute("CREATE TABLE IF NOT EXISTS CAIXA (id INTEGER PRIMARY KEY AUTOINCREMENT, data_abertura TEXT NOT NULL, data_fechamento TEXT, saldo_inicial REAL DEFAULT 0, saldo_final REAL, troco_deixado REAL DEFAULT 0, usuario_abertura_id INTEGER, status TEXT DEFAULT 'ABERTO')").connection.commit()
        db.execute("CREATE TABLE IF NOT EXISTS FORMAS_PAGAMENTO (id INTEGER PRIMARY KEY AUTOINCREMENT, nome TEXT NOT NULL UNIQUE, ativo INTEGER DEFAULT 1)").connection.commit()
        db.execute("CREATE TABLE IF NOT EXISTS DESPESAS (id INTEGER PRIMARY KEY AUTOINCREMENT, descricao TEXT, valor REAL, data_vencimento TEXT, data_pagamento TEXT, categoria TEXT, recorrente INTEGER, observacao TEXT, status TEXT DEFAULT 'PENDENTE')").connection.commit()
        db.execute("CREATE TABLE IF NOT EXISTS RECEITAS (id INTEGER PRIMARY KEY AUTOINCREMENT, descricao TEXT, valor REAL, data_vencimento TEXT, data_recebimento TEXT, categoria TEXT, recorrente INTEGER, observacao TEXT, status TEXT DEFAULT 'PENDENTE')").connection.commit()


        def check_and_add(table, col, type_def):
            try:
                cols = [i[1] for i in db.execute(f"PRAGMA table_info({table})").fetchall()]
                if col not in cols: db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {type_def}").connection.commit()
            except Exception as e:
                print(f"Erro ao adicionar coluna {col} em {table}: {e}") # Log de erro


        # [MODIFICADO] Adicionando CPF/CNPJ, E-MAIL, Endereço e COMPLEMENTO ao CLIENTES
        campos_clientes = [
            'is_whatsapp','marca_veiculo','modelo_veiculo','cor_veiculo','observacoes',
            'tipo_veiculo','tipo_cliente','plano_mensal','regra_inicio','data_inicio_ciclo',
            'data_fim_ciclo', 'cpf_cnpj', 'email', 'logradouro', 'numero', 'bairro', 'cidade', 'estado', 'cep',
            'complemento' # NOVO CAMPO ADICIONADO AQUI
        ]
        for c in campos_clientes:
            check_and_add('CLIENTES', c, 'TEXT')
            
        for c in ['is_eletrico','is_suv']: check_and_add('CLIENTES', c, 'INTEGER DEFAULT 0')
        for c in ['mensal_diurno','mensal_noturno','mensal_integral']: check_and_add('TARIFAS', c, 'REAL DEFAULT 0')
        check_and_add('TICKETS', 'numero_sequencial', 'INTEGER'); check_and_add('TICKETS', 'local_vaga', 'TEXT')
        check_and_add('USUARIOS', 'ultimo_acesso', 'TEXT')
        
        # [MODIFICADO] Adicionando campos de ticket personalizado, horario e flags de impressao automatica
        check_and_add('ESTABELECIMENTO', 'mensagem_ticket', 'TEXT')
        check_and_add('ESTABELECIMENTO', 'exibir_mensagem', 'INTEGER DEFAULT 0')
        check_and_add('ESTABELECIMENTO', 'horario_funcionamento', 'TEXT')
        check_and_add('ESTABELECIMENTO', 'imprimir_entrada_avulso', 'INTEGER DEFAULT 0')
        check_and_add('ESTABELECIMENTO', 'imprimir_entrada_mensalista', 'INTEGER DEFAULT 0')


        if db.execute("SELECT COUNT(*) FROM FORMAS_PAGAMENTO").fetchone()[0] == 0:
            for f in ['Dinheiro', 'Pix', 'Cartão de Débito', 'Cartão de Crédito']: db.execute("INSERT INTO FORMAS_PAGAMENTO (nome) VALUES (?)", (f,)).connection.commit()
        if db.execute("SELECT COUNT(*) FROM ESTABELECIMENTO").fetchone()[0] == 0:
            db.execute("INSERT INTO ESTABELECIMENTO (nome, total_vagas) VALUES (?, ?)", ("ParkSystem Pro", 50)).connection.commit()
        if db.execute("SELECT COUNT(*) FROM TARIFAS").fetchone()[0] == 0:
            db.execute("INSERT INTO TARIFAS (valor_carro, valor_moto, teto_diaria, tolerancia_minutos, mensal_diurno, mensal_noturno, mensal_integral) VALUES (10, 5, 50, 15, 150, 120, 250)").connection.commit()
        if db.execute("SELECT COUNT(*) FROM USUARIOS").fetchone()[0] == 0:
            db.execute("INSERT INTO USUARIOS (nome, username, senha, perfil) VALUES (?, ?, ?, ?)", ('Super Admin', 'admin', generate_password_hash('admin', method='pbkdf2:sha256'), 'ADMIN')).connection.commit()
        db.commit()


# --- CÁLCULOS ---
def calcular_tempo_e_valor(hora_entrada_str, hora_saida_str=None, tipo_veiculo='CARRO', placa=None):
    db = get_db(); config = db.execute("SELECT * FROM TARIFAS LIMIT 1").fetchone()
    val_carro = config['valor_carro'] if config else 10.0; val_moto = config['valor_moto'] if config else 5.0
    teto = config['teto_diaria'] if config else 50.0; tol = (config['tolerancia_minutos'] * 60) if config else 900


    if placa:
        cli = db.execute("SELECT * FROM CLIENTES WHERE placa = ?", (placa,)).fetchone()
        if cli and cli['tipo_cliente'] == 'MENSALISTA':
            if cli['data_fim_ciclo']:
                try:
                    # O filtro to_datetime_filter corrigido garante que isso funcione
                    if to_datetime_filter(cli['data_fim_ciclo']).date() >= obter_data_br(): return 0, 0.00
                except: pass
            elif cli['regra_inicio'] == 'IMEDIATO': return 0, 0.00


    h_sai = obter_hora_br() if not hora_saida_str else datetime.strptime(hora_saida_str, '%Y-%m-%d %H:%M:%S')
    h_ent = datetime.strptime(hora_entrada_str, '%Y-%m-%d %H:%M:%S')
    seg = (h_sai - h_ent).total_seconds(); seg = 0 if seg < 0 else seg
    if seg <= tol: return 0, 0.00
    val_hr = val_moto if tipo_veiculo == 'MOTO' else val_carro
    hrs = math.ceil(seg / 3600) if seg > 0 else 1
    return hrs, min(hrs * val_hr, teto) if hrs <= 24 else math.ceil(hrs / 24) * teto


# --- ROTAS GERAIS ---
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        db = get_db()
        u = db.execute("SELECT * FROM USUARIOS WHERE username = ?", (request.form['username'],)).fetchone()
        
        if u and check_password_hash(u['senha'], request.form['senha']):
            session['user_id'] = u['id']; session['user_nome'] = u['nome']; session['user_perfil'] = u['perfil']
            
            # 🚨 ATUALIZANDO O CAMPO ultimo_acesso NO LOGIN 🚨
            agora_str = obter_hora_br().strftime('%Y-%m-%d %H:%M:%S')
            db.execute("UPDATE USUARIOS SET ultimo_acesso = ? WHERE id = ?", (agora_str, u['id'])).connection.commit()
            
            return redirect(url_for('home'))
        flash('Login inválido.', 'danger')
    return render_template('login.html')


@app.route('/logout')
def logout(): session.clear(); return redirect(url_for('login'))


@app.route('/')
def index(): return redirect(url_for('home')) if 'user_id' in session else redirect(url_for('login'))


@app.route('/home')
@login_required
def home():
    db=get_db(); qtde=db.execute("SELECT COUNT(*) FROM TICKETS WHERE status='ESTACIONADO'").fetchone()[0]
    total=db.execute("SELECT total_vagas FROM ESTABELECIMENTO").fetchone()['total_vagas']
    return render_template('home.html', qtde=qtde, livres=total-qtde, total_vagas=total)


# [NOVA ROTA - GUIA DE ESTILO]
@app.route('/guia_estilo')
@login_required
@admin_required
def guia_estilo():
    return render_template('guia_estilo.html')


# [NOVA ROTA - TESTE DE COMPONENTES]
@app.route('/teste_componentes')
@login_required
@admin_required
def teste_componentes():
    return render_template('teste_componentes.html')


@app.route('/caixa/abrir', methods=['GET','POST'])
@login_required
def abrir_caixa():
    if request.method=='POST':
        get_db().execute("INSERT INTO CAIXA (data_abertura, saldo_inicial, usuario_abertura_id, status) VALUES (?,?,?,?)", (obter_hora_br().strftime('%Y-%m-%d %H:%M:%S'), safe_float(request.form['saldo_inicial']), session['user_id'], 'ABERTO')).connection.commit()
        return redirect(url_for('home'))
    return render_template('abrir_caixa.html', sugestao=0.0)


@app.route('/caixa/fechar', methods=['GET','POST'])
@login_required
def fechar_caixa():
    db=get_db(); cx=db.execute("SELECT * FROM CAIXA WHERE status='ABERTO'").fetchone()
    tot=db.execute("SELECT SUM(valor_total) FROM TICKETS WHERE status='PAGO' AND caixa_id=?",(cx['id'],)).fetchone()[0] or 0
    if request.method=='POST':
        if tot > 0:
            obs = f"Fechamento do Caixa #{cx['id']} (Operador: {session['user_nome']})"
            db.execute("INSERT INTO RECEITAS (descricao, valor, data_vencimento, data_recebimento, categoria, recorrente, observacao, status) VALUES (?,?,?,?,?,?,?,?)",
                        (f"Fechamento Caixa #{cx['id']}", tot, obter_data_br().strftime('%Y-%m-%d'), obter_data_br().strftime('%Y-%m-%d'), 'Fechamento de Caixa', 0, obs, 'RECEBIDO')).connection.commit()
    
        db.execute("UPDATE CAIXA SET data_fechamento=?, saldo_final=?, troco_deixado=?, status='FECHADO' WHERE id=?", (obter_hora_br().strftime('%Y-%m-%d %H:%M:%S'), cx['saldo_inicial']+tot, safe_float(request.form['troco_deixado']), cx['id'])).connection.commit()
        return redirect(url_for('abrir_caixa'))
    return render_template('fechar_caixa.html', caixa=cx, total_vendas=tot, saldo_final_esperado=cx['saldo_inicial']+tot)


# [MODIFICADO] API de Consulta (Segurança de Tipo)
@app.route('/api/consultar_placa/<string:placa>')
@login_required
def api_consultar_placa(placa):
    db=get_db(); p=placa.upper().replace('-','').strip()
    # Busca tipo histórico para segurança
    hist_type = get_historical_type(db, p)
    c=db.execute("SELECT * FROM CLIENTES WHERE placa=?",(p,)).fetchone()
    # Define o tipo que será travado no frontend (prioridade: histórico > cadastro > padrão)
    locked_type = hist_type or (c['tipo_veiculo'] if c else 'CARRO')
    if c or hist_type:
        return jsonify({
            'encontrado':True,
            'origem':'CADASTRO' if c else 'HISTORICO',
            'tipo_veiculo':locked_type,
            'nome':c['nome'] if c else 'Visitante Recorrente',
            'tipo_cliente':c['tipo_cliente'] if c else 'AVULSO'
        })
    return jsonify({'encontrado':False})


# [MODIFICADO] Entrada (Auditoria + Redirecionamento Automático + TRAVA DE LOTAÇÃO)
@app.route('/entrada', methods=['GET','POST'])
@login_required
@caixa_required
def dar_entrada():
    if request.method=='POST':
        db = get_db()
        
        # 1. VERIFICAÇÃO DE CAPACIDADE (TRAVA DE SEGURANÇA)
        ocupadas = db.execute("SELECT COUNT(*) FROM TICKETS WHERE status='ESTACIONADO'").fetchone()[0]
        
        # Busca todas as colunas do ESTABELECIMENTO para carregar flags de impressão.
        estab = db.execute("SELECT * FROM ESTABELECIMENTO WHERE id=1").fetchone()
        
        total_vagas = estab['total_vagas'] if estab else 0
        
        if ocupadas >= total_vagas:
            flash(f'LOTAÇÃO MÁXIMA ATINGIDA! Não é possível dar entrada. ({ocupadas}/{total_vagas})', 'danger')
            return redirect(url_for('home'))


        # 2. PROSSEGUE COM A LÓGICA DE ENTRADA NORMAL
        p=request.form['placa'].upper().replace('-','').strip(); t=request.form.get('tipo','CARRO'); l=request.form.get('local_vaga','').upper()
        
        # Verifica se carro JÁ está no pátio (duplicidade)
        if db.execute("SELECT 1 FROM TICKETS WHERE placa=? AND status='ESTACIONADO'",(p,)).fetchone():
            flash('Este veículo JÁ está no pátio!','warning')
            return redirect(url_for('dar_entrada'))
        
        cli=db.execute("SELECT * FROM CLIENTES WHERE placa=?",(p,)).fetchone()
        
        # Tipo regra: se mensalista, usa MENSALISTA. Se avulso, usa o tipo informado (t)
        tipo_reg = cli['tipo_cliente'] if cli and cli['tipo_cliente'] == 'MENSALISTA' else t
        
        if cli and cli['tipo_cliente']=='MENSALISTA':
            hj=obter_data_br()
            if cli['data_fim_ciclo'] and to_datetime_filter(cli['data_fim_ciclo']).date() < hj: # Usando filtro corrigido
                flash('Mensalidade Vencida! Cobrando avulso.','danger'); tipo_reg = t
            elif cli['regra_inicio']=='PRIMEIRO_USO' and not cli['data_inicio_ciclo']:
                db.execute("UPDATE CLIENTES SET data_inicio_ciclo=?, data_fim_ciclo=? WHERE id=?",(obter_data_br().strftime('%Y-%m-%d'), (obter_data_br()+timedelta(days=30)).strftime('%Y-%m-%d'), cli['id'])).connection.commit(); flash('Mensalista ativado!','success')
        
        # GRAVA O TIPO ESCOLHIDO (t) se não for mensalista, para auditar
        final_type = t if not (cli and cli['tipo_cliente'] == 'MENSALISTA') else tipo_reg
        
        ns = None if tipo_reg=='MENSALISTA' else 0
        cursor = db.execute("INSERT INTO TICKETS (placa,hora_entrada,status,tipo,local_vaga,numero_sequencial) VALUES (?,?,?,?,?,?)",(p,obter_hora_br().strftime('%Y-%m-%d %H:%M:%S'),'ESTACIONADO',final_type,l,ns))
        new_ticket_id = cursor.lastrowid # Pega ID para redirecionar se precisar imprimir
        db.commit()
        
        # --- LÓGICA DE REDIRECIONAMENTO PARA IMPRESSÃO ---
        deve_imprimir = False
        
        # CORREÇÃO: Usando colchetes [] em vez de .get()
        if tipo_reg == 'MENSALISTA':
            if estab['imprimir_entrada_mensalista']: deve_imprimir = True
        else: # Avulso (Carro ou Moto)
            if estab['imprimir_entrada_avulso']: deve_imprimir = True
            
        if deve_imprimir:
            # Redireciona para a impressão com flag automática
            return redirect(url_for('imprimir_entrada', id=new_ticket_id, auto=1))


        return redirect(url_for('listar_estacionados'))
    return render_template('form_entrada.html')


@app.route('/estacionados')
@login_required
def listar_estacionados():
    db=get_db(); conf=db.execute("SELECT * FROM TARIFAS LIMIT 1").fetchone(); tar={'valor_carro':conf['valor_carro'],'valor_moto':conf['valor_moto'],'tolerancia_minutos':conf['tolerancia_minutos'],'teto_diaria':conf['teto_diaria']}
    tkts=db.execute("SELECT T.*,C.id as cid,C.tipo_cliente FROM TICKETS T LEFT JOIN CLIENTES C ON T.placa=C.placa WHERE T.status='ESTACIONADO' ORDER BY T.hora_entrada DESC").fetchall()
    lst=[]
    for t in tkts:
        im=(t['tipo_cliente']=='MENSALISTA' and calcular_tempo_e_valor(t['hora_entrada'],None,t['tipo'],t['placa'])[1]==0)
        lst.append({'id':t['id'],'ticket_numero':gerar_codigo_visual(t['numero_sequencial'],t['cid'],t['tipo_cliente']),'placa':fmt_placa(t['placa']),'tipo':t['tipo'],'local':t['local_vaga'],'entrada':fmt_data(t['hora_entrada']),'valor_a_pagar':calcular_tempo_e_valor(t['hora_entrada'],None,t['tipo'],t['placa'])[1],'is_mensalista_ativo':im})
    return render_template('listar_estacionados.html',tickets=lst,tarifas=tar)


# [NOVA ROTA - PASSO 2] Busca Rápida para Saída (Scanner)
@app.route('/buscar_saida', methods=['POST'])
@login_required
def buscar_saida():
    termo = request.form.get('termo', '').strip().upper()
    if not termo: return redirect(url_for('listar_estacionados'))
    db = get_db()
    # Limpa formatação para busca (remove hifens, espaços)
    termo_limpo = termo.replace('-', '').replace(' ', '')
    # 1. Tenta buscar por PLACA (Exata)
    t = db.execute("SELECT id FROM TICKETS WHERE replace(placa, '-', '') = ? AND status='ESTACIONADO'", (termo_limpo,)).fetchone()
    # 2. Se não achou e for número, tenta pelo SEQUENCIAL do ticket (Ex: escaneou '0045')
    if not t and termo_limpo.isdigit():
        t = db.execute("SELECT id FROM TICKETS WHERE numero_sequencial = ? AND status='ESTACIONADO'", (int(termo_limpo),)).fetchone()
    # 3. Tenta buscar pelo código visual completo (Ex: 'TCK-0045')
    if not t and '-' in termo:
        try:
            seq = int(termo.split('-')[1])
            t = db.execute("SELECT id FROM TICKETS WHERE numero_sequencial = ? AND status='ESTACIONADO'", (seq,)).fetchone()
        except: pass


    if t:
        # Achou! Vai direto para a tela de cobrança
        return redirect(url_for('visualizar_pagamento', id=t['id']))
    else:
        flash(f'Veículo não encontrado no pátio: {termo}', 'warning')
        return redirect(url_for('listar_estacionados'))


@app.route('/saida/<int:id>')
@login_required
@caixa_required
def visualizar_pagamento(id):
    t=get_db().execute("SELECT T.*,C.id as cid,C.tipo_cliente FROM TICKETS T LEFT JOIN CLIENTES C ON T.placa=C.placa WHERE T.id=?",(id,)).fetchone()
    h,v=calcular_tempo_e_valor(t['hora_entrada'],None,t['tipo'],t['placa'])
    return render_template('confirmacao_pagamento.html',ticket_id=id,valor=v,placa=fmt_placa(t['placa']),tipo=t['tipo'],entrada=fmt_data(t['hora_entrada']),saida=obter_hora_br().strftime('%d/%m/%Y %H:%M:%S'),ticket_numero=gerar_codigo_visual(t['numero_sequencial'],t['cid'],t['tipo_cliente']),tempo_em_horas=h,formas_pagamento=get_db().execute("SELECT * FROM FORMAS_PAGAMENTO WHERE ativo=1").fetchall())


@app.route('/pagar/<int:id>', methods=['POST'])
@login_required
@caixa_required
def finalizar_pagamento(id):
    db=get_db(); t=db.execute("SELECT T.*,C.id as cid,C.tipo_cliente FROM TICKETS T LEFT JOIN CLIENTES C ON T.placa=C.placa WHERE T.id=?",(id,)).fetchone()
    pg=request.form.get('forma_pagamento','Dinheiro'); sai=obter_hora_br().strftime('%Y-%m-%d %H:%M:%S')
    _,v=calcular_tempo_e_valor(t['hora_entrada'],sai,t['tipo'],t['placa'])
    prox_ns = db.execute("SELECT MAX(numero_sequencial) FROM TICKETS").fetchone()[0] or 0
    prox_ns += 1
    if t['tipo_cliente']=='MENSALISTA': pg='Mensalista'
    elif v==0: pg='Tolerância'
    db.execute("UPDATE TICKETS SET hora_saida=?,valor_total=?,status='PAGO',caixa_id=?,forma_pagamento=?, numero_sequencial=? WHERE id=?",
                (sai, v, db.execute("SELECT id FROM CAIXA WHERE status='ABERTO'").fetchone()['id'], pg, prox_ns, id)).connection.commit()
            
    if t['tipo_cliente']=='MENSALISTA': return redirect(url_for('listar_estacionados'))
    return render_template('recibo_pagamento.html',ticket_id=id,valor=v,placa=fmt_placa(t['placa']),entrada=fmt_data(t['hora_entrada']),saida=fmt_data(sai),ticket_numero=gerar_codigo_visual(prox_ns,t['cid'],t['tipo_cliente']),forma_pagto=pg)


# --- ROTA DE TESTE DE FILTRO ---
@app.route('/filtro_teste')
@login_required
@admin_required
def filtro_teste():
    """
    Simula o retorno de dados do financeiro com PENDENTE e PAGO para debug.
    """
    from flask import request, render_template, g
    # Dados de teste que imitam a estrutura de DESPESAS/RECEITAS
    dados_teste = [
        {'id': 1, 'descricao': 'Teste PENDENTE Antigo', 'valor': 100.00, 'data_vencimento': '2000-01-01', 'status': 'PENDENTE'},
        {'id': 2, 'descricao': 'Teste PAGO Certo', 'valor': 50.00, 'data_vencimento': '2024-11-20', 'status': 'PAGO'},
        {'id': 3, 'descricao': 'Teste PENDENTE Novo', 'valor': 25.00, 'data_vencimento': '2025-12-31', 'status': 'PENDENTE'},
        {'id': 4, 'descricao': 'Teste RECEBIDO (Receita)', 'valor': 10.00, 'data_vencimento': '2025-11-20', 'status': 'RECEBIDO'},
    ]
    
    # Simula as variáveis de estado do filtro (como viriam da URL ou padrão)
    status_selecionado = request.args.get('status', 'PENDENTE') # Padrão PENDENTE
    
    dados_filtrados = []
    
    if status_selecionado == 'TODOS':
        dados_filtrados = dados_teste
    else:
        for item in dados_teste:
            if item['status'] == status_selecionado:
                dados_filtrados.append(item)

    return render_template('filtro_teste.html',
                            dados=dados_filtrados,
                            status_selecionado=status_selecionado)


# --- FINANCEIRO ---
@app.route('/financeiro', methods=['GET', 'POST'])
@login_required
@admin_required
def financeiro():
    db = get_db()
    hoje = obter_data_br(); primeiro_dia = hoje.replace(day=1)
    passado_distante = PASSADO_DISTANTE_SQL # Busca desde 1900-01-01
    active_tab = request.args.get('tab', request.values.get('active_tab', 'mensal'))
    
    # ---------------------------------------------------------------------------------
    # --- Variáveis de ESTADO e DADOS para a ABA RELATÓRIOS (INICIALIZAÇÃO) ---
    total_geral = 0
    totais_pgto = {'Dinheiro': 0, 'Pix': 0, 'Cartão': 0}
    dados_agrupados = {}
    formas_pagamento = db.execute("SELECT * FROM FORMAS_PAGAMENTO WHERE ativo=1").fetchall()
    
    # Filtros de Relatório (Inicializa com valores vazios/padrão)
    filtro_termo = ''
    filtro_inicio_date = ''
    filtro_hora_inicio = '00:00'
    filtro_fim_date = ''
    filtro_hora_fim = '23:59'
    filtro_tipo = 'TODOS'
    filtro_pgto = 'TODOS'

    # Se a chamada foi feita APÓS o filtro do relatório, carrega os dados da SESSION
    if active_tab == 'relatorios' and request.args.get('relatorio_carregado') == '1':
        # Carrega dados e filtros da sessão e dos parâmetros passados pela rota /relatorios
        total_geral = session.pop('rel_total_geral', 0)
        totais_pgto = session.pop('rel_totais_pgto', {'Dinheiro': 0, 'Pix': 0, 'Cartão': 0})
        dados_agrupados = session.pop('rel_dados_agrupados', {})
        
        # Carrega filtros da Query String (passados no redirecionamento)
        filtro_termo = request.args.get('termo', '')
        filtro_inicio_date = request.args.get('data_inicio', '')
        filtro_hora_inicio = request.args.get('hora_inicio', '00:00')
        filtro_fim_date = request.args.get('data_fim', '')
        filtro_hora_fim = request.args.get('hora_fim', '23:59')
        filtro_tipo = request.args.get('tipo_cliente', 'TODOS')
        filtro_pgto = request.args.get('forma_pagamento', 'TODOS')
    
    # ---------------------------------------------------------------------------------


    # Filtros Vendas (Default: Mês Atual)
    v_ini_form = request.values.get('vendas_ini'); v_fim_form = request.values.get('vendas_fim')
    v_ini = v_ini_form if v_ini_form else primeiro_dia.strftime('%Y-%m-%d'); v_fim = v_fim_form if v_fim_form else hoje.strftime('%Y-%m-%d')


    # Filtros Receitas (Default: PENDENTE + Histórico)
    car_ini_form = request.values.get('car_ini'); car_fim_form = request.values.get('car_fim'); car_status = request.values.get('car_status', 'PENDENTE')
    car_ini = car_ini_form if car_ini_form else passado_distante; car_fim = car_fim_form if car_fim_form else MAX_SQL_DATE
    # Filtros Despesas (Default: PENDENTE + Histórico)
    cap_ini_form = request.values.get('cap_ini'); cap_fim_form = request.values.get('cap_fim'); cap_status = request.values.get('cap_status', 'PENDENTE')
    cap_ini = cap_ini_form if cap_ini_form else passado_distante; cap_fim = cap_fim_form if cap_fim_form else MAX_SQL_DATE
    # Filtros Caixas (Default: Histórico)
    c_ini_form = request.values.get('caixa_ini'); c_fim_form = request.values.get('caixa_fim'); c_op = request.values.get('caixa_op', 'TODOS')
    c_ini = c_ini_form if c_ini_form else passado_distante; c_fim = c_fim_form if c_fim_form else hoje.strftime('%Y-%m-%d')


    # --- CONSULTAS ---
    # 1. Despesas
    q_desp = "SELECT * FROM DESPESAS WHERE data_vencimento BETWEEN ? AND ?"; p_desp = [cap_ini, cap_fim]
    if cap_status != 'TODOS': q_desp += " AND status = ?"; p_desp.append(cap_status)
    q_desp += " ORDER BY CASE status WHEN 'PENDENTE' THEN 0 ELSE 1 END, data_vencimento ASC"    
    despesas = db.execute(q_desp, p_desp).fetchall()
    
    # 2. Receitas
    q_rec = "SELECT * FROM RECEITAS WHERE data_vencimento BETWEEN ? AND ?"; p_rec = [car_ini, car_fim]
    if car_status != 'TODOS':
        s = 'RECEBIDO' if car_status == 'RECEBIDO' else 'PENDENTE'
        q_rec += " AND status = ?"; p_rec.append(s)
    q_rec += " ORDER BY CASE status WHEN 'PENDENTE' THEN 0 ELSE 1 END, data_vencimento ASC"
    receitas = db.execute(q_rec, p_rec).fetchall()
    
    # 3. Mensalistas
    mensalistas = db.execute("SELECT * FROM CLIENTES WHERE tipo_cliente='MENSALISTA' ORDER BY data_fim_ciclo").fetchall()


    # 4. Caixas
    q_cx = """
        SELECT 
            C.*,
            U.nome as nome_operador,
            (SELECT SUM(valor_total) FROM TICKETS WHERE caixa_id=C.id AND status='PAGO') as vendas 
        FROM CAIXA C 
        LEFT JOIN USUARIOS U ON C.usuario_abertura_id=U.id 
        WHERE date(C.data_abertura) BETWEEN ? AND ?
    """
    p_cx = [c_ini, c_fim]

    if c_op != 'TODOS': 
        q_cx += " AND C.usuario_abertura_id=?"
        p_cx.append(c_op)
    
    q_cx += " ORDER BY C.id DESC"
    
    cx_raw = db.execute(q_cx, p_cx).fetchall()
    
    # Inicializa variáveis de Caixas e Totais
    caixas = [] 
    tot_v_cx = 0 
    tot_f_cx = 0 
    
    for c in cx_raw:
        v=c['vendas'] or 0; sf=c['saldo_final'] if c['saldo_final'] is not None else (c['saldo_inicial']+v)
        tot_v_cx+=v; tot_f_cx+=sf
        caixas.append({'id':c['id'],'operador':c['nome_operador'] or 'Sistema', 'abertura':fmt_data(c['data_abertura']), 'fechamento':fmt_data(c['data_fechamento']), 'status':c['status'], 'saldo_inicial':c['saldo_inicial'], 'vendas':v, 'saldo_final':sf})


    # 5. VENDAS (TICKETS)
    q_tkts = "SELECT T.*, C.nome as nome_cliente, C.id as cid, C.tipo_cliente FROM TICKETS T LEFT JOIN CLIENTES C ON T.placa = C.placa WHERE T.status = 'PAGO' AND date(T.hora_saida) BETWEEN ? AND ? ORDER BY T.hora_saida DESC"
    raw_tickets = db.execute(q_tkts, [v_ini, v_fim]).fetchall()
    tickets_agrupados = {};
    for t in raw_tickets:
        cod = gerar_codigo_visual(t['numero_sequencial'], t['cid'], t['tipo_cliente']); sv = t['forma_pagamento'];
        if t['tipo_cliente'] == 'MENSALISTA': sv = 'Mensalista'
        elif t['valor_total'] == 0: sv = 'Tolerância'
        d = {'ticket': cod, 'placa': fmt_placa(t['placa']), 'nome_cliente': t['nome_cliente'] or 'Avulso', 'entrada': fmt_data(t['hora_entrada']), 'saida': fmt_data(t['hora_saida']), 'valor': t['valor_total'], 'pgto': t['forma_pagamento'], 'status_visual': sv}
        k = fmt_placa(t['placa'])
        tickets_agrupados.setdefault(k, {'placa': k, 'nome_cliente': d['nome_cliente'], 'tickets': [], 'total': 0.0})
        tickets_agrupados[k]['tickets'].append(d); tickets_agrupados[k]['total'] += t['valor_total']


    # TOTAIS GERAIS
    sum_receitas = sum(r['valor'] for r in receitas if r['status']=='RECEBIDO' and r['categoria']!='Fechamento de Caixa')
    sum_despesas = sum(d['valor'] for d in despesas if d['status']=='PAGO')
    ini_mes_str = primeiro_dia.strftime('%Y-%m-%d'); hj_str = hoje.strftime('%Y-%m-%d')
    ent_hj = db.execute("SELECT SUM(valor_total) FROM TICKETS WHERE status='PAGO' AND date(hora_saida)=?", (hj_str,)).fetchone()[0] or 0.0
    
    # [MODIFICADO - RECEITA MENSAL POR CAIXA FECHADO]
    # 1. Soma o valor dos tickets pagos no mês que pertencem a CAIXAS JÁ FECHADOS (FECHADO, FECHADO_AUTO)
    q_tkt_fechado = """
        SELECT SUM(T.valor_total)
        FROM TICKETS T
        JOIN CAIXA C ON T.caixa_id = C.id
        WHERE T.status = 'PAGO'    
            AND date(T.hora_saida) >= ?
            AND C.status IN ('FECHADO', 'FECHADO_AUTO')
    """
    tkt_mes_fechado = db.execute(q_tkt_fechado, (ini_mes_str,)).fetchone()[0] or 0.0

    # 2. Receitas Avulsas recebidas no mês (rec_mes)
    rec_mes = db.execute("SELECT SUM(valor) FROM RECEITAS WHERE status='RECEBIDO' AND categoria != 'Fechamento de Caixa' AND date(data_recebimento)>=?", (ini_mes_str,)).fetchone()[0] or 0.0
    
    # A Receita Mensal (Tickets) passa a ser SÓ o que está em caixas fechados
    tkt_mes = tkt_mes_fechado
    
    # A Entrada Mensal (para o banner) = Tickets Fechados + Receitas Avulsas
    entradas_mes = tkt_mes + rec_mes
    # FIM [MODIFICADO]
    
    saidas_mes = db.execute("SELECT SUM(valor) FROM DESPESAS WHERE status='PAGO' AND date(data_pagamento) >= ?", (ini_mes_str,)).fetchone()[0] or 0.0


    sum_tkt_filtered = sum(t['valor_total'] for t in raw_tickets)
    total_entradas = sum_tkt_filtered + sum_receitas
    saldo_periodo = total_entradas - sum_despesas
    
    # [NOVOS CÁLCULOS DE RESUMO PARA AS ABAS]
    total_rec_pendente_filtrado = sum(r['valor'] for r in receitas if r['status']=='PENDENTE')
    total_rec_recebido_filtrado = sum(r['valor'] for r in receitas if r['status']=='RECEBIDO')
    
    total_desp_pendente_filtrado = sum(d['valor'] for d in despesas if d['status']=='PENDENTE')
    total_desp_pago_filtrado = sum(d['valor'] for d in despesas if d['status']=='PAGO')
    # FIM [NOVOS CÁLCULOS]


    return render_template('financeiro.html',
                            mensalistas=mensalistas, despesas=despesas, receitas=receitas, caixas=caixas, tickets_agrupados=tickets_agrupados,
                            total_entradas=total_entradas, total_saidas=sum_despesas, saldo_periodo=saldo_periodo,
                            entradas_hoje=ent_hj, entradas_mes=entradas_mes, saidas_mes=saidas_mes,
                        
                            # Filtros Visuais (Mantém o estado da seleção)
                            vendas_ini=v_ini_form, vendas_fim=v_fim_form,
                            car_ini=car_ini_form, car_fim=car_fim_form, car_status=car_status,
                            cap_ini=cap_ini_form, cap_fim=cap_fim_form, cap_status=cap_status,
                            caixa_ini=c_ini_form, caixa_fim=c_fim_form, caixa_op=c_op,
                        
                            total_vendas_caixa=tot_v_cx, total_acumulado_caixa=tot_f_cx,
                            active_tab=active_tab,
                            tarifas=db.execute("SELECT * FROM TARIFAS LIMIT 1").fetchone(),
                            formas=db.execute("SELECT * FROM FORMAS_PAGAMENTO WHERE ativo=1").fetchall(),
                            usuarios=db.execute("SELECT * FROM USUARIOS").fetchall(),
                        
                            # NOVOS TOTAIS DE FILTRO POR ABA
                            total_rec_pendente_filtrado=total_rec_pendente_filtrado,
                            total_rec_recebido_filtrado=total_rec_recebido_filtrado,
                            total_desp_pendente_filtrado=total_desp_pendente_filtrado,
                            total_desp_pago_filtrado=total_desp_pago_filtrado,
                            
                            # VARIÁVEIS DO RELATÓRIO
                            total_geral=total_geral,
                            totais_pgto=totais_pgto,
                            dados_agrupados=dados_agrupados,
                            filtro_termo=filtro_termo,
                            filtro_inicio_date=filtro_inicio_date,
                            filtro_hora_inicio=filtro_hora_inicio,
                            filtro_fim_date=filtro_fim_date,
                            filtro_hora_fim=filtro_hora_fim,
                            filtro_tipo=filtro_tipo,
                            filtro_pgto=filtro_pgto,
                            formas_pagamento=formas_pagamento,
                            )


@app.route('/financeiro/receber_mensalidade/<int:id>', methods=['POST'])
@login_required
def receber_mensalidade_financeiro(id):
    db=get_db(); cx=db.execute("SELECT id FROM CAIXA WHERE status='ABERTO'").fetchone()
    if not cx: flash('Abra o caixa.','danger'); return redirect(url_for('financeiro', tab='mensal'))
    cli=db.execute("SELECT * FROM CLIENTES WHERE id=?",(id,)).fetchone()
    hj = obter_data_br(); dt_fim = to_datetime_filter(cli['data_fim_ciclo']).date() if cli['data_fim_ciclo'] else hj
    nv = (hj if dt_fim < hj else dt_fim) + timedelta(days=30)
    db.execute("UPDATE CLIENTES SET data_fim_ciclo=? WHERE id=?",(nv.strftime('%Y-%m-%d'),id)).connection.commit()
    db.execute("INSERT INTO TICKETS (placa,tipo,hora_entrada,hora_saida,valor_total,status,caixa_id,forma_pagamento) VALUES (?,'MENSALIDADE',?,?,?,'PAGO',?,?)", (cli['placa'],obter_hora_br().strftime('%Y-%m-%d %H:%M:%S'),obter_hora_br().strftime('%Y-%m-%d %H:%M:%S'),safe_float(request.form['valor']),cx['id'],request.form['forma_pagamento'])).connection.commit()
    return redirect(url_for('financeiro', status='PENDENTE', tab='mensal'))


@app.route('/financeiro/despesa/nova', methods=['POST'])
@login_required
def nova_despesa():
    desc = request.form['descricao']; val = safe_float(request.form['valor']); venc = request.form['vencimento']
    cat = request.form['categoria']; obs = request.form.get('observacao', '')
    recorrencia = request.form.get('tipo_recorrencia', 'NÃO'); qtd_total = int(request.form.get('qtd_repeticoes', 1))
    get_db().execute("INSERT INTO DESPESAS (descricao,valor,data_vencimento,categoria,recorrente,observacao,status) VALUES (?,?,?,?,?,?,?)", (desc,val,venc,cat,1 if recorrencia!='NÃO' else 0,obs,'PENDENTE')).connection.commit()
    if recorrencia != 'NÃO' and qtd_total > 1:
        d_base = datetime.strptime(venc, '%Y-%m-%d').date()
        for i in range(1, qtd_total):
            if recorrencia == 'MENSAL': n_data = add_months(d_base, i)
            elif recorrencia == 'SEMANAL': n_data = d_base + timedelta(weeks=i)
            elif recorrencia == 'QUINZENAL': n_data = d_base + timedelta(weeks=2 * i)
            elif recorrencia == 'DIARIA': n_data = d_base + timedelta(days=i)
            elif recorrencia == 'ANUAL': n_data = add_months(d_base, i*12)
            get_db().execute("INSERT INTO DESPESAS (descricao, valor, data_vencimento, categoria, recorrente, observacao, status) VALUES (?, ?, ?, ?, ?, ?, ?)", (f"{desc} ({i+1}/{qtd_total})", val, n_data.strftime('%Y-%m-%d'), cat, 1, obs, 'PENDENTE')).connection.commit()
    return redirect(url_for('financeiro', status='PENDENTE', tab='cap'))


# [MODIFICADO - AUDITORIA DE DATA E ADIANTAMENTO]
@app.route('/financeiro/despesa/pagar/<int:id>')
@login_required
def pagar_despesa(id):
    db = get_db()
    # Busca a despesa para verificar a data de vencimento e a observação original
    despesa = db.execute("SELECT data_vencimento, observacao FROM DESPESAS WHERE id=?", (id,)).fetchone()
    if not despesa:    
        flash('Despesa não encontrada.', 'danger')
        return redirect(url_for('financeiro', tab='cap'))

    data_vencimento_obj = datetime.strptime(despesa['data_vencimento'], '%Y-%m-%d').date()
    data_baixa_obj = obter_data_br()
    
    observacao_original = despesa['observacao'] or ''
    observacao_nova = observacao_original
    
    # A data de pagamento/baixa será a data de hoje (obter_data_br())
    data_pagamento_str = data_baixa_obj.strftime('%Y-%m-%d')
    
    # Assume a data de vencimento original como data do registro (Fallback)
    data_registro_str = data_vencimento_obj.strftime('%Y-%m-%d')
    
    # 1. Verifica Adiantamento
    if data_baixa_obj < data_vencimento_obj:
        data_original_str = data_vencimento_obj.strftime('%d/%m/%Y')
        sinalizacao = f"(Lançamento baixado adiantado. Vencimento original: {data_original_str})"
        
        observacao_nova += ("\n" if observacao_original else "") + sinalizacao
        
        # Atualiza data_vencimento para HOJE e data_pagamento para HOJE
        data_registro_str = data_baixa_obj.strftime('%Y-%m-%d')
        
        db.execute("UPDATE DESPESAS SET status='PAGO', data_pagamento=?, observacao=?, data_vencimento=? WHERE id=?",
                    (data_pagamento_str, observacao_nova, data_registro_str, id)).connection.commit()
    
    # 2. Verifica Atraso (Lógica já existente e priorizada se for na data ou após)
    elif data_baixa_obj > data_vencimento_obj:
        # Lançamento ocorre após a data de vencimento (Atraso detectado)
        data_original_str = data_vencimento_obj.strftime('%d/%m/%Y')
        sinalizacao = f"(Lançamento realizado após a data original: {data_original_str})"
        
        # Adiciona a sinalização, quebrando linha se já houver observação
        observacao_nova += ("\n" if observacao_original else "") + sinalizacao
        
        # Atualiza a data de VENCIMENTO para HOJE (requisito de "data mude para a atual data de baixa")
        data_registro_str = data_baixa_obj.strftime('%Y-%m-%d')    
        
        db.execute("UPDATE DESPESAS SET status='PAGO', data_pagamento=?, observacao=?, data_vencimento=? WHERE id=?",
                    (data_pagamento_str, observacao_nova, data_registro_str, id)).connection.commit()
    
    # 3. Na data (Na data de vencimento): Mantém data_vencimento original.
    else:
        # Mantém a data de vencimento original para data_pagamento/data_vencimento
        db.execute("UPDATE DESPESAS SET status='PAGO', data_pagamento=?, observacao=? WHERE id=?",
                    (data_registro_str, observacao_nova, id)).connection.commit()
        
    return redirect(url_for('financeiro', status='PENDENTE', tab='cap'))


@app.route('/financeiro/despesa/excluir/<int:id>/<string:modo>')
@login_required
def excluir_despesa(id, modo):
    db = get_db()
    if modo == 'unico': db.execute("DELETE FROM DESPESAS WHERE id=?", (id,)).connection.commit()
    elif modo == 'todos':
        alvo = db.execute("SELECT descricao, categoria FROM DESPESAS WHERE id=?", (id,)).fetchone()
        if alvo:
            raiz = re.sub(r' \(\d+/\d+\)$', '', alvo['descricao'])
            db.execute("DELETE FROM DESPESAS WHERE id >= ? AND categoria = ? AND status = 'PENDENTE' AND (descricao = ? OR descricao LIKE ?)", (id, alvo['categoria'], raiz, f"{raiz} (%")).connection.commit()
    db.commit(); return redirect(url_for('financeiro', tab='cap'))


@app.route('/financeiro/despesa/editar/<int:id>', methods=['POST'])
@login_required
def editar_despesa(id):
    get_db().execute("UPDATE DESPESAS SET descricao=?, valor=?, data_vencimento=?, categoria=?, observacao=? WHERE id=?", (request.form['descricao'],safe_float(request.form['valor']),request.form['vencimento'],request.form['categoria'],request.form['observacao'],id)).connection.commit()
    return redirect(url_for('financeiro', tab='cap'))


@app.route('/financeiro/receita/nova', methods=['POST'])
@login_required
def nova_receita():
    desc = request.form['descricao']; val = safe_float(request.form['valor']); venc = request.form['vencimento']
    cat = request.form['categoria']; obs = request.form.get('observacao', '')
    recorrencia = request.form.get('tipo_recorrencia', 'NÃO'); qtd_total = int(request.form.get('qtd_repeticoes', 1))


    get_db().execute("INSERT INTO RECEITAS (descricao,valor,data_vencimento,categoria,recorrente,observacao,status) VALUES (?,?,?,?,?,?,?)", (desc,val,venc,cat,1 if recorrencia!='NÃO' else 0,obs,'PENDENTE')).connection.commit()
    if recorrencia != 'NÃO' and qtd_total > 1:
        data_base = datetime.strptime(venc, '%Y-%m-%d').date()
        for i in range(1, qtd_total):
            if recorrencia == 'MENSAL': n_data = add_months(data_base, i)
            elif recorrencia == 'SEMANAL': n_data = data_base + timedelta(weeks=i)
            elif recorrencia == 'QUINZENAL': n_data = data_base + timedelta(weeks=2 * i)
            elif recorrencia == 'DIARIA': n_data = data_base + timedelta(days=i)
            elif recorrencia == 'ANUAL': n_data = add_months(data_base, i*12)
            get_db().execute("INSERT INTO RECEITAS (descricao, valor, data_vencimento, categoria, recorrente, observacao, status) VALUES (?, ?, ?, ?, ?, ?, ?)", (f"{desc} ({i+1}/{qtd_total})", val, n_data.strftime('%Y-%m-%d'), cat, 1, obs, 'PENDENTE')).connection.commit()
    return redirect(url_for('financeiro', status='PENDENTE', tab='car'))


# [MODIFICADO - AUDITORIA DE DATA E ADIANTAMENTO]
@app.route('/financeiro/receita/receber/<int:id>')
@login_required
def receber_receita(id):
    db = get_db()
    # Busca a receita para verificar a data de vencimento e a observação original
    receita = db.execute("SELECT data_vencimento, observacao FROM RECEITAS WHERE id=?", (id,)).fetchone()
    if not receita:    
        flash('Receita não encontrada.', 'danger')
        return redirect(url_for('financeiro', tab='car'))
    
    data_vencimento_obj = datetime.strptime(receita['data_vencimento'], '%Y-%m-%d').date()
    data_baixa_obj = obter_data_br()
    
    observacao_original = receita['observacao'] or ''
    observacao_nova = observacao_original
    
    # A data de recebimento/baixa será a data de hoje (obter_data_br())
    data_recebimento_str = data_baixa_obj.strftime('%Y-%m-%d')
    
    # Assume a data de vencimento original como data do registro
    data_registro_str = data_vencimento_obj.strftime('%Y-%m-%d')
    
    # 1. Verifica Adiantamento
    if data_baixa_obj < data_vencimento_obj:
        data_original_str = data_vencimento_obj.strftime('%d/%m/%Y')
        sinalizacao = f"(Lançamento baixado adiantado. Vencimento original: {data_original_str})"
        
        observacao_nova += ("\n" if observacao_original else "") + sinalizacao
        
        # Atualiza data_vencimento para HOJE e data_recebimento para HOJE
        data_registro_str = data_baixa_obj.strftime('%Y-%m-%d')    
        
        db.execute("UPDATE RECEITAS SET status='RECEBIDO', data_recebimento=?, observacao=?, data_vencimento=? WHERE id=?",
                    (data_recebimento_str, observacao_nova, data_registro_str, id)).connection.commit()
    
    # 2. Verifica Atraso (Lógica já existente e priorizada se for na data ou após)
    elif data_baixa_obj > data_vencimento_obj:
        # Lançamento ocorre após a data de vencimento (Atraso detectado)
        data_original_str = data_vencimento_obj.strftime('%d/%m/%Y')
        sinalizacao = f"(Lançamento realizado após a data original: {data_original_str})"
        
        # Adiciona a sinalização, quebrando linha se já houver observação
        observacao_nova += ("\n" if observacao_original else "") + sinalizacao
        
        # Atualiza a data de VENCIMENTO para HOJE (requisito de "data mude para a atual data de baixa")
        data_registro_str = data_baixa_obj.strftime('%Y-%m-%d')    
        
        db.execute("UPDATE RECEITAS SET status='RECEBIDO', data_recebimento=?, observacao=?, data_vencimento=? WHERE id=?",
                    (data_recebimento_str, observacao_nova, data_registro_str, id)).connection.commit()
    
    # 3. Na data (Na data de vencimento): Mantém data_vencimento original.
    else:
        # Mantém a data de vencimento original para data_recebimento/data_vencimento
        db.execute("UPDATE RECEITAS SET status='RECEBIDO', data_recebimento=?, observacao=? WHERE id=?",
                    (data_registro_str, observacao_nova, id)).connection.commit()

    return redirect(url_for('financeiro', status='PENDENTE', tab='car'))


@app.route('/financeiro/receita/excluir/<int:id>/<string:modo>')
@login_required
def excluir_receita(id, modo):
    db = get_db()
    if modo == 'unico': db.execute("DELETE FROM RECEITAS WHERE id=?", (id,)).connection.commit()
    elif modo == 'todos':
        alvo = db.execute("SELECT descricao, categoria FROM RECEITAS WHERE id=?", (id,)).fetchone()
        if alvo:
            raiz = re.sub(r' \(\d+/\d+\)$', '', alvo['descricao'])
            db.execute("DELETE FROM RECEITAS WHERE id >= ? AND categoria = ? AND status = 'PENDENTE' AND (descricao = ? OR descricao LIKE ?)", (id, alvo['categoria'], raiz, f"{raiz} (%")).connection.commit()
    db.commit(); return redirect(url_for('financeiro', tab='car'))


@app.route('/financeiro/receita/editar/<int:id>', methods=['POST'])
@login_required
def editar_receita(id):
    get_db().execute("UPDATE RECEITAS SET descricao=?, valor=?, data_vencimento=?, categoria=?, observacao=? WHERE id=?", (request.form['descricao'],safe_float(request.form['valor']),request.form['vencimento'],request.form['categoria'],request.form['observacao'],id)).connection.commit()
    return redirect(url_for('financeiro', tab='car'))


# [MODIFICADO] Relatórios com Filtros 4 Campos e Auditoria (Processa e Redireciona)
@app.route('/relatorios', methods=['GET', 'POST'])
@login_required
def relatorios():
    db = get_db()
    
    # Se for GET e não for um redirecionamento, redireciona para a aba no financeiro (Comportamento antigo de rota única)
    if request.method == 'GET' and not request.args.get('relatorio_carregado'):
        # Redireciona para o financeiro para carregar a aba de relatórios vazia ou com valores default
        return redirect(url_for('financeiro', active_tab='relatorios'))

    # Se for POST (o filtro foi submetido) ou GET (após um redirecionamento)
    dt_ini_form = request.values.get('data_inicio'); hora_ini_form = request.values.get('hora_inicio')
    dt_fim_form = request.values.get('data_fim'); hora_fim_form = request.values.get('hora_fim')
    termo = request.values.get('termo', '').strip()
    tipo_filtro = request.values.get('tipo_cliente', 'TODOS')
    pgto_filtro = request.values.get('forma_pagamento', 'TODOS')

    hoje_date = datetime.now(BR_TZ).strftime('%Y-%m-%d')
    sql_ini = f"{dt_ini_form if dt_ini_form else hoje_date} {parse_time_input(hora_ini_form) or '00:00'}:00"
    sql_fim = f"{dt_fim_form if dt_fim_form else hoje_date} {parse_time_input(hora_fim_form) or '23:59'}:00"


    q = """
        SELECT T.*, C.nome as nome_cliente, C.id as cliente_id, C.tipo_cliente, C.tipo_veiculo as tipo_cadastrado
        FROM TICKETS T
        LEFT JOIN CLIENTES C ON T.placa = C.placa
        WHERE T.status = 'PAGO'
          AND T.hora_saida >= ?
          AND T.hora_saida <= ?
        ORDER BY T.hora_saida DESC
    """
    params = [sql_ini, sql_fim]
    
    # Lógica de termo de busca
    if termo: 
        termo_limpo = re.sub(r'[^0-9A-Z]','',termo.upper())
        q = q.replace("WHERE", "WHERE (T.placa LIKE ? OR C.nome LIKE ? OR T.numero_sequencial = ?) AND")
        
        # Coloca os parâmetros do termo ANTES dos parâmetros de data
        params = [f"%{termo_limpo}%", f"%{termo}%", int(re.sub(r'[^0-9]','',termo) or -1)] + params 

    if tipo_filtro == 'MENSALISTA': q += " AND T.tipo = 'MENSALIDADE'"
    elif tipo_filtro == 'AVULSO': q += " AND T.tipo != 'MENSALIDADE'"
    
    if pgto_filtro != 'TODOS':
        if pgto_filtro == 'Tolerância': q += " AND (T.forma_pagamento = 'Tolerância' OR T.valor_total = 0)"
        else: q += " AND T.forma_pagamento = ?"; params.append(pgto_filtro)


    raw = db.execute(q, params).fetchall()
    dados_agrupados = {}; tot_geral = 0; totais_pgto = {'Dinheiro': 0, 'Pix': 0, 'Cartão': 0}
    
    for t in raw:
        tot_geral += t['valor_total']; fp = t['forma_pagamento']
        if 'Dinheiro' in fp: totais_pgto['Dinheiro'] += t['valor_total']
        elif 'Pix' in fp: totais_pgto['Pix'] += t['valor_total']
        elif 'Cartão' in fp: totais_pgto['Cartão'] += t['valor_total']
        
        # --- LÓGICA DE AUDITORIA ---
        divergencia = False
        tipo_esperado = t['tipo_cadastrado']
        if not tipo_esperado:
            hist_tkt = db.execute("SELECT tipo FROM TICKETS WHERE placa = ? AND status='PAGO' AND id != ? ORDER BY hora_saida DESC LIMIT 1", (t['placa'], t['id'])).fetchone()
            if hist_tkt: tipo_esperado = hist_tkt['tipo']
        
        if tipo_esperado and t['tipo'] != 'MENSALIDADE':
            if str(t['tipo']).upper() != str(tipo_esperado).upper():
                if t['tipo_cliente'] != 'MENSALISTA': divergencia = True

        d = {'ticket': gerar_codigo_visual(t['numero_sequencial'], t['cliente_id'], t['tipo_cliente']),
             'placa': fmt_placa(t['placa']),
             'nome_cliente': t['nome_cliente'] or 'Avulso',
             'entrada': fmt_data(t['hora_entrada']),
             'saida': fmt_data(t['hora_saida']),
             'valor': t['valor_total'],
             'pgto': t['forma_pagamento'],
             'status_visual': 'Mensalista' if t['tipo_cliente']=='MENSALISTA' else ('Tolerância' if t['valor_total']==0 else t['forma_pagamento']),
             'divergencia': divergencia,
             'tipo_cobrado': t['tipo'],
             'tipo_cadastrado': tipo_esperado or 'N/A'}
        
        k = fmt_placa(t['placa'])
        dados_agrupados.setdefault(k, {'placa': k, 'nome_cliente': d['nome_cliente'], 'tipo_cliente': t['tipo_cliente'], 'total_pago_periodo': 0.0, 'tickets': []})
        dados_agrupados[k]['tickets'].append(d); dados_agrupados[k]['total_pago_periodo'] += t['valor_total']

    # 🚨 NOVO: SALVA RESULTADOS NA SESSÃO 🚨
    # A session é o único lugar seguro para guardar a estrutura de dados complexa (dados_agrupados)
    session['rel_total_geral'] = tot_geral
    session['rel_totais_pgto'] = totais_pgto
    session['rel_dados_agrupados'] = dados_agrupados
    
    # Redireciona para o financeiro, passando os filtros e a flag de carregado na Query String
    return redirect(url_for('financeiro',
                            active_tab='relatorios',
                            relatorio_carregado='1', # Flag para o financeiro saber que deve carregar da sessão
                            termo=termo,
                            data_inicio=dt_ini_form,
                            hora_inicio=hora_ini_form,
                            data_fim=dt_fim_form,
                            hora_fim=hora_fim_form,
                            tipo_cliente=tipo_filtro,
                            forma_pagamento=pgto_filtro
                            ))


@app.route('/relatorios/caixas/detalhes/<int:id>')
@login_required
@admin_required
def detalhes_caixa(id):
    db=get_db(); cx=db.execute("SELECT C.*,U.nome as operador_nome FROM CAIXA C LEFT JOIN USUARIOS U ON C.usuario_abertura_id=U.id WHERE C.id=?",(id,)).fetchone()
    if not cx: return redirect(url_for('financeiro', tab='caixa'))
    vendas=db.execute("SELECT SUM(valor_total) FROM TICKETS WHERE status='PAGO' AND caixa_id=?",(id,)).fetchone()[0] or 0
    saldo_atual=cx['saldo_final'] if cx['saldo_final'] is not None else (cx['saldo_inicial']+vendas)
    pgs={r['forma_pagamento']:r['total'] for r in db.execute("SELECT forma_pagamento,SUM(valor_total) as total FROM TICKETS WHERE status='PAGO' AND caixa_id=? GROUP BY forma_pagamento",(id,)).fetchall()}
    # Query Aprimorada: Traz tipo_cadastrado para auditoria
    raw=db.execute("SELECT T.*,C.nome as nome_cliente,C.id as cid,C.tipo_cliente,C.tipo_veiculo as tipo_cadastrado FROM TICKETS T LEFT JOIN CLIENTES C ON T.placa=C.placa WHERE T.id > 0 AND T.status='PAGO' AND T.caixa_id=? ORDER BY T.hora_saida DESC",(id,)).fetchall()
    avu={}; men={}
    for t in raw:
        # --- LÓGICA DE AUDITORIA (DIVERGÊNCIA) ---
        divergencia = False
        tipo_hist = t['tipo_cadastrado'] or get_historical_type(db, t['placa'])
        if tipo_hist and t['tipo'] != 'MENSALIDADE' and str(t['tipo']).upper() != str(tipo_hist).upper():
            if t['tipo_cliente'] != 'MENSALISTA': divergencia = True


        d={'ticket':gerar_codigo_visual(t['numero_sequencial'],t['cid'],t['tipo_cliente']),
           'placa':fmt_placa(t['placa']),
           'nome_cliente':t['nome_cliente'],
           'entrada':fmt_data(t['hora_entrada']),
           'saida':fmt_data(t['hora_saida']),
           'valor':t['valor_total'],
           'pgto':t['forma_pagamento'],
           'status_visual': 'Mensalista' if t['tipo_cliente']=='MENSALISTA' else ('Tolerância' if t['valor_total']==0 else t['forma_pagamento']),
           'divergencia': divergencia, # Passa flag para o template
           'tipo_cobrado': t['tipo'],
           'tipo_cadastrado': tipo_hist
          }
        
        tgt=men if t['tipo_cliente']=='MENSALISTA' else avu; k=fmt_placa(t['placa'])
        tgt.setdefault(k,{'placa':k,'nome':d['nome_cliente'] or 'Avulso','tickets':[],'total':0}); tgt[k]['tickets'].append(d); tgt[k]['total']+=t['valor_total']
        
    return render_template('detalhes_caixa.html', caixa=cx, avulsos=avu, mensalistas=men, total_vendas=vendas, saldo_atual=saldo_atual, resumo_pagamentos=pgs)


@app.route('/relatorios/caixas', methods=['GET'])
def relatorio_caixas_redirect(): return redirect(url_for('financeiro', tab='caixa'))


@app.route('/historico')
@login_required
def historico():
    tkts = get_db().execute("SELECT T.*, C.id as cliente_id, C.tipo_cliente FROM TICKETS T LEFT JOIN CLIENTES C ON T.placa = C.placa WHERE T.status='PAGO' ORDER BY T.hora_saida DESC LIMIT 50").fetchall()
    lista = [{'ticket_numero': gerar_codigo_visual(t['numero_sequencial'], t['cliente_id'], t['tipo_cliente']), 'placa':fmt_placa(t['placa']), 'tipo':t['tipo'], 'entrada':fmt_data(t['hora_entrada']), 'saida':fmt_data(t['hora_saida']), 'valor_total':t['valor_total']} for t in tkts]
    return render_template('listar_historico.html', historico=lista)


@app.route('/imprimir/<int:id>')
@login_required
def imprimir_ticket(id):
    t = get_db().execute("SELECT T.*, C.id as cid, C.tipo_cliente FROM TICKETS T LEFT JOIN CLIENTES C ON T.placa = C.placa WHERE T.id=?",(id,)).fetchone()
    estab = get_db().execute("SELECT * FROM ESTABELECIMENTO LIMIT 1").fetchone()
    # CORRECAO DO NONE TYPE ERROR: Garante que valor_total seja 0.0 se for None
    valor = t['valor_total'] if t['valor_total'] is not None else 0.0
    return render_template('recibo_impressao.html', valor=valor, placa=fmt_placa(t['placa']), entrada=fmt_data(t['hora_entrada']), saida=fmt_data(t['hora_saida']), ticket_numero=gerar_codigo_visual(t['numero_sequencial'], t['cid'], t['tipo_cliente']), estab=estab)


# [NOVA ROTA] Imprimir Ticket de Entrada
@app.route('/imprimir_entrada/<int:id>')
@login_required
def imprimir_entrada(id):
    db = get_db()
    t = db.execute("SELECT T.*, C.id as cid, C.tipo_cliente FROM TICKETS T LEFT JOIN CLIENTES C ON T.placa = C.placa WHERE T.id=?",(id,)).fetchone()
    estab = get_db().execute("SELECT * FROM ESTABELECIMENTO LIMIT 1").fetchone()
    tarifas = db.execute("SELECT * FROM TARIFAS LIMIT 1").fetchone()
    # Verifica se foi chamado automaticamente pela entrada
    auto_print = request.args.get('auto', '0') == '1'
    return render_template('ticket_entrada.html',
                            placa=fmt_placa(t['placa']),
                            entrada=fmt_data(t['hora_entrada']),
                            ticket_numero=gerar_codigo_visual(t['numero_sequencial'], t['cid'], t['tipo_cliente']),
                            estab=estab,
                            tarifas=tarifas,
                            auto_print=auto_print)


@app.route('/configuracoes', methods=['GET','POST'])
@login_required
@admin_required
def configuracoes():
    db = get_db()
    if request.method == 'POST':
        # --- Adicionar Pagamento ---
        if 'add_pagamento' in request.form:
            db.execute("INSERT INTO FORMAS_PAGAMENTO (nome) VALUES (?)", (request.form['nova_forma'],)).connection.commit()
        
        # --- Remover Pagamento ---
        elif 'del_pagamento' in request.form:
            db.execute("DELETE FROM FORMAS_PAGAMENTO WHERE id=?", (request.form['id_forma'],)).connection.commit()
        
        # --- Salvar Tarifas ---
        elif 'save_tarifas' in request.form:
            db.execute("""
                UPDATE TARIFAS SET
                valor_carro=?, valor_moto=?, teto_diaria=?, tolerancia_minutos=?,
                mensal_diurno=?, mensal_noturno=?, mensal_integral=?
                WHERE id=1
            """, (
                safe_float(request.form['valor_carro']),
                safe_float(request.form['valor_moto']),
                safe_float(request.form['teto_diaria']),
                int(request.form['tolerancia_minutos']),
                safe_float(request.form['mensal_diurno']),
                safe_float(request.form['mensal_noturno']),
                safe_float(request.form['mensal_integral'])
            )).connection.commit()
        
        # --- Salvar Dados da Empresa ---
        elif 'save_empresa' in request.form:
            try:
                db.execute("""
                    UPDATE ESTABELECIMENTO SET
                    nome=?, cnpj=?, endereco=?, telefone=?, total_vagas=?, horario_funcionamento=?
                    WHERE id=1
                """, (
                    request.form['estab_nome'],
                    request.form['estab_cnpj'],
                    request.form['estab_endereco'],
                    request.form['estab_telefone'],
                    int(request.form['estab_vagas']),
                    request.form.get('estab_horario', '')
                )).connection.commit()
            except Exception as e:
                # Se der erro de coluna faltando, cria e tenta de novo
                if 'no such column' in str(e):
                    db.execute("ALTER TABLE ESTABELECIMENTO ADD COLUMN horario_funcionamento TEXT").connection.commit()
                    db.execute("""
                        UPDATE ESTABELECIMENTO SET
                        nome=?, cnpj=?, endereco=?, telefone=?, total_vagas=?, horario_funcionamento=?
                        WHERE id=1
                    """, (
                        request.form['estab_nome'], request.form['estab_cnpj'], request.form['estab_endereco'],
                        request.form['estab_telefone'], int(request.form['estab_vagas']), request.form.get('estab_horario', '')
                    )).connection.commit()


        # --- Salvar Configurações de Impressão ---
        elif 'save_impressao' in request.form:
            msg = request.form.get('mensagem_ticket', '')
            ativo = 1 if 'exibir_mensagem' in request.form else 0
            
            imp_avulso = 1 if 'imprimir_entrada_avulso' in request.form else 0
            imp_mensalista = 1 if 'imprimir_entrada_mensalista' in request.form else 0
            
            try:
                db.execute("UPDATE ESTABELECIMENTO SET mensagem_ticket=?, exibir_mensagem=?, imprimir_entrada_avulso=?, imprimir_entrada_mensalista=? WHERE id=1", (msg, ativo, imp_avulso, imp_mensalista)).connection.commit()
            except Exception as e:
                if 'no such column' in str(e):
                    try: db.execute("ALTER TABLE ESTABELECIMENTO ADD COLUMN mensagem_ticket TEXT").connection.commit()
                    except: pass
                    try: db.execute("ALTER TABLE ESTABELECIMENTO ADD COLUMN exibir_mensagem INTEGER DEFAULT 0").connection.commit()
                    except: pass
                    try: db.execute("ALTER TABLE ESTABELECIMENTO ADD COLUMN imprimir_entrada_avulso INTEGER DEFAULT 0").connection.commit()
                    except: pass
                    try: db.execute("ALTER TABLE ESTABELECIMENTO ADD COLUMN imprimir_entrada_mensalista INTEGER DEFAULT 0").connection.commit()
                    except: pass
                    
                    db.execute("UPDATE ESTABELECIMENTO SET mensagem_ticket=?, exibir_mensagem=?, imprimir_entrada_avulso=?, imprimir_entrada_mensalista=? WHERE id=1", (msg, ativo, imp_avulso, imp_mensalista)).connection.commit()


        return redirect(url_for('configuracoes'))
    # Renderização da página
    return render_template('configuracoes.html',
                            conf=db.execute("SELECT * FROM TARIFAS").fetchone(),
                            estab=db.execute("SELECT * FROM ESTABELECIMENTO").fetchone(),
                            formas=db.execute("SELECT * FROM FORMAS_PAGAMENTO WHERE ativo=1").fetchall())


@app.route('/clientes')
@login_required
def listar_clientes(): return render_template('listar_clientes.html', clientes=get_db().execute("SELECT * FROM CLIENTES ORDER BY nome").fetchall())


# [MODIFICADO] ROTA NOVO CLIENTE
@app.route('/clientes/novo', methods=['GET','POST'])
@login_required
def novo_cliente():
    if request.method=='POST':
        # Capturamos o formulário em uma variável para reutilizar em caso de erro
        form_data = request.form

        # 1. Tratamento de Datas
        dt_ini, dt_fim = None, None
        if form_data.get('tipo_cliente')=='MENSALISTA' and form_data.get('regra_inicio')=='IMEDIATO':
            dt_ini = obter_data_br().strftime('%Y-%m-%d')
            dt_fim = (obter_data_br()+timedelta(days=30)).strftime('%Y-%m-%d')
        
        # 2. Validação CPF/CNPJ (A CORREÇÃO ESTÁ AQUI)
        cpf_cnpj = form_data.get('cpf_cnpj')
        if cpf_cnpj:
            is_valid, msg = validar_cpf_cnpj(cpf_cnpj)
            if not is_valid:
                flash(f'Falha na validação: {msg}', 'danger')
                # MUDANÇA: Em vez de redirect, renderizamos a página de volta com os dados!
                return render_template('form_cliente.html', cliente=form_data)

        try:
            # Tratamento seguro da placa
            placa = (form_data.get('placa') or '').upper().replace('-','').strip()

            get_db().execute(
                "INSERT INTO CLIENTES (nome,telefone,is_whatsapp,placa,marca_veiculo,modelo_veiculo,cor_veiculo,observacoes,is_eletrico,is_suv,tipo_veiculo,tipo_cliente,plano_mensal,regra_inicio,data_inicio_ciclo,data_fim_ciclo,cpf_cnpj,email,logradouro,numero,bairro,cidade,estado,cep,complemento) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    form_data.get('nome'),
                    form_data.get('telefone'),
                    1 if form_data.get('is_whatsapp') else 0,
                    placa,
                    form_data.get('marca_veiculo'),
                    form_data.get('modelo_veiculo'),
                    form_data.get('cor_veiculo'),
                    form_data.get('observacoes'),
                    1 if form_data.get('is_eletrico') else 0,
                    1 if form_data.get('is_suv') else 0,
                    form_data.get('tipo_veiculo'),
                    form_data.get('tipo_cliente'),
                    form_data.get('plano_mensal'),
                    form_data.get('regra_inicio'),
                    dt_ini, dt_fim,
                    cpf_cnpj,
                    form_data.get('email'),
                    form_data.get('logradouro'),
                    form_data.get('numero'),
                    form_data.get('bairro'),
                    form_data.get('cidade'),
                    form_data.get('estado'),
                    form_data.get('cep'),
                    form_data.get('complemento')
                )
            ).connection.commit()
            
            registrar_log('CLIENTES', f"Cadastrou cliente: {form_data.get('nome')}")
            return redirect(url_for('listar_clientes'))

        except Exception as e:
            # Tratamento de erro de banco (Duplicidade, etc)
            if "UNIQUE constraint failed" in str(e):
                 flash(f'Erro: A placa {placa} já está cadastrada.', 'warning')
            else:
                 flash(f'Erro técnico ao cadastrar: {e}', 'danger')
            
            # Retorna para o formulário mantendo os dados
            return render_template('form_cliente.html', cliente=form_data)

    return render_template('form_cliente.html', cliente=None)


# [MODIFICADO] ROTA EDITAR CLIENTE
@app.route('/clientes/editar/<int:id>', methods=['GET','POST'])
@login_required
def editar_cliente(id):
    db = get_db()
    c = db.execute("SELECT * FROM CLIENTES WHERE id=?", (id,)).fetchone()
    
    if request.method == 'POST':
        # Cria um dicionário com os dados enviados para repopular o form em caso de erro
        form_data = dict(request.form)
        form_data['id'] = id # Garante que o ID exista para a URL do form funcionar

        # 1. Validação de CPF/CNPJ
        cpf_cnpj = request.form.get('cpf_cnpj')
        if cpf_cnpj:
            is_valid, msg = validar_cpf_cnpj(cpf_cnpj)
            if not is_valid:
                flash(f'Falha na validação: {msg}', 'danger')
                # CORREÇÃO: Renderiza a página novamente com os dados (não perde o que digitou)
                return render_template('form_cliente.html', cliente=form_data)

        try:
            # 2. Tratamento Blindado da Placa (Evita Erro 400)
            # Se o campo vier do form, usa ele. Se vier vazio (disabled), usa o do banco (c['placa'])
            placa_nova = request.form.get('placa')
            if placa_nova:
                placa_final = placa_nova.upper().replace('-', '').strip()
            else:
                placa_final = c['placa'] # Mantém a original

            db.execute(
                "UPDATE CLIENTES SET nome=?, telefone=?, is_whatsapp=?, placa=?, marca_veiculo=?, modelo_veiculo=?, cor_veiculo=?, observacoes=?, is_eletrico=?, is_suv=?, tipo_veiculo=?, tipo_cliente=?, plano_mensal=?, cpf_cnpj=?, email=?, logradouro=?, numero=?, bairro=?, cidade=?, estado=?, cep=?, complemento=? WHERE id=?",
                (
                    request.form.get('nome'),
                    request.form.get('telefone'),
                    1 if request.form.get('is_whatsapp') else 0,
                    placa_final,
                    request.form.get('marca_veiculo'),
                    request.form.get('modelo_veiculo'),
                    request.form.get('cor_veiculo'),
                    request.form.get('observacoes'),
                    1 if request.form.get('is_eletrico') else 0,
                    1 if request.form.get('is_suv') else 0,
                    request.form.get('tipo_veiculo'),
                    request.form.get('tipo_cliente'),
                    request.form.get('plano_mensal'),
                    cpf_cnpj,
                    request.form.get('email'),
                    request.form.get('logradouro'),
                    request.form.get('numero'),
                    request.form.get('bairro'),
                    request.form.get('cidade'),
                    request.form.get('estado'),
                    request.form.get('cep'),
                    request.form.get('complemento'),
                    id
                )
            ).connection.commit()

            registrar_log('CLIENTES', f"Editou cliente ID {id}: {request.form.get('nome')}")
            flash('Cliente atualizado com sucesso!', 'success')
            return redirect(url_for('listar_clientes'))

        except Exception as e:
            # Tratamento de erro de banco (Duplicidade, etc)
            if "UNIQUE constraint failed" in str(e):
                 flash(f'Erro: A placa informada já pertence a outro cliente.', 'warning')
            else:
                 logging.error(f"Erro ao editar cliente {id}: {e}")
                 flash(f'Erro técnico ao salvar: {e}', 'danger')
            
            # Retorna para o formulário mantendo os dados
            return render_template('form_cliente.html', cliente=form_data)

    return render_template('form_cliente.html', cliente=c)


@app.route('/clientes/excluir/<int:id>')
@login_required
def excluir_cliente(id): get_db().execute("DELETE FROM CLIENTES WHERE id=?",(id,)).connection.commit(); return redirect(url_for('listar_clientes'))


@app.route('/usuarios')
@login_required
@admin_required
def listar_usuarios(): return render_template('listar_usuarios.html', usuarios=get_db().execute("SELECT * FROM USUARIOS").fetchall())


# [NOVA ROTA] Edição administrativa de usuário (Permitir editar Exceto Admin 1)
@app.route('/usuarios/editar', methods=['POST'])
@login_required
@admin_required
def editar_usuario():
    id_usuario = request.form['id_usuario']
    nome = request.form['nome']
    username = request.form['username']
    perfil = request.form['perfil']
    
    # 1. Trava de Segurança: Não permite editar o Admin principal (ID 1)
    if int(id_usuario) == 1:
        flash('Não é permitido editar o Administrador principal (ID 1).', 'danger')
        return redirect(url_for('listar_usuarios'))
        
    db = get_db()
    # 2. Verifica se o novo username já existe para outro usuário
    existing_user = db.execute("SELECT id FROM USUARIOS WHERE username = ? AND id != ?", (username, id_usuario)).fetchone()
    if existing_user:
        flash('Username já está em uso.', 'danger')
        return redirect(url_for('listar_usuarios'))

    db.execute("UPDATE USUARIOS SET nome=?, username=?, perfil=? WHERE id=?", (nome, username, perfil, id_usuario)).connection.commit()
    flash(f'Usuário {nome} atualizado com sucesso.', 'success')
    return redirect(url_for('listar_usuarios'))


@app.route('/usuarios/novo', methods=['POST'])
@login_required
@admin_required
def novo_usuario(): get_db().execute("INSERT INTO USUARIOS (nome,username,senha,perfil) VALUES (?,?,?,?)",(request.form['nome'],request.form['username'],generate_password_hash(request.form['senha'],method='pbkdf2:sha256'),request.form['perfil'])).connection.commit(); return redirect(url_for('listar_usuarios'))


@app.route('/usuarios/excluir/<int:id>')
@login_required
@admin_required
def excluir_usuario(id):
    if id!=1 and id!=session['user_id']: get_db().execute("DELETE FROM USUARIOS WHERE id=?",(id,)).connection.commit()
    return redirect(url_for('listar_usuarios'))


@app.route('/usuarios/resetar_senha', methods=['POST'])
@login_required
@admin_required
def admin_resetar_senha(): get_db().execute("UPDATE USUARIOS SET senha=? WHERE id=?",(generate_password_hash(request.form['nova_senha'],method='pbkdf2:sha256'),request.form['id_usuario'])).connection.commit(); return redirect(url_for('listar_usuarios'))


# [MODIFICADA] Perfil: Lógica ajustada para não exigir nova senha ao editar nome
@app.route('/perfil', methods=['GET','POST'])
@login_required
def perfil():
    db = get_db()
    u = db.execute("SELECT * FROM USUARIOS WHERE id=?", (session['user_id'],)).fetchone()
    
    if request.method == 'POST':
        novo_nome = request.form.get('nome')
        novo_username = request.form.get('username')
        senha_atual = request.form.get('senha_atual')
        nova_senha = request.form.get('nova_senha')
        confirmar_senha = request.form.get('confirmar_senha')

        # 1. Blindagem: Se campos vierem vazios (ex: cache), mantém os atuais
        if not novo_nome: novo_nome = u['nome']
        if not novo_username: novo_username = u['username']

        # 2. Segurança: Senha ATUAL é obrigatória para qualquer alteração
        if not senha_atual or not check_password_hash(u['senha'], senha_atual):
            flash('Para salvar qualquer alteração, digite sua senha atual corretamente.', 'danger')
            return render_template('perfil.html', usuario=u)

        # 3. Validação de Username Único (se foi alterado)
        if novo_username != u['username']:
            exists = db.execute("SELECT id FROM USUARIOS WHERE username = ? AND id != ?", (novo_username, session['user_id'])).fetchone()
            if exists:
                flash(f'O login "{novo_username}" já está em uso.', 'danger')
                return render_template('perfil.html', usuario=u)

        # 4. Atualizar Dados Cadastrais (Baseado no Perfil)
        if session['user_perfil'] == 'ADMIN':
            db.execute("UPDATE USUARIOS SET nome=?, username=? WHERE id=?", (novo_nome, novo_username, session['user_id'])).connection.commit()
            session['user_nome'] = novo_nome
        else:
            # Operador só altera username, nome permanece o original do banco
            db.execute("UPDATE USUARIOS SET username=? WHERE id=?", (novo_username, session['user_id'])).connection.commit()

        # 5. Atualizar Senha (SOMENTE se o usuário preencheu o campo)
        msg_extra = ""
        if nova_senha:
            if nova_senha == confirmar_senha and len(nova_senha) >= 1:
                db.execute("UPDATE USUARIOS SET senha=? WHERE id=?", (generate_password_hash(nova_senha, method='pbkdf2:sha256'), session['user_id'])).connection.commit()
                msg_extra = " e Senha"
            else:
                flash('Dados atualizados, mas a SENHA NÃO foi alterada (vazia ou confirmação incorreta).', 'warning')
                return redirect(url_for('perfil'))

        flash(f'Perfil{msg_extra} atualizado com sucesso!', 'success')
        
        # Recarrega dados
        u = db.execute("SELECT * FROM USUARIOS WHERE id=?", (session['user_id'],)).fetchone()
        return redirect(url_for('perfil'))
        
    return render_template('perfil.html', usuario=u)


if __name__ == '__main__':
    init_db()
    # Para usar o Webview no futuro, descomente a linha abaixo e comente o app.run
    # webview.create_window('Sistema de Gestão', app, min_size=(1024, 768)); webview.start()
    app.run(debug=True, port=5000)