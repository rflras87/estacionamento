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

# --- CONFIGURAÇÃO ---
app = Flask(__name__)
app.config['SECRET_KEY'] = 'segredo_v10_platinum_final_production' 
DATABASE = 'estacionamento.db' 
BR_TZ = pytz.timezone('America/Sao_Paulo')
MAX_SQL_DATE = '2100-01-01'

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

# [NOVO] Helper para Relatórios: Converte HHMM em HH:MM
def parse_time_input(time_str):
    if not time_str: return None
    clean = time_str.replace(':', '').strip()
    if len(clean) == 4 and clean.isdigit():
        h, m = clean[:2], clean[2:]
        if int(h) > 23 or int(m) > 59: return "00:00"
        return f"{h}:{m}"
    return time_str

# [NOVO] Helper de Segurança: Busca tipo histórico confiável
def get_historical_type(db, placa):
    # 1. Checa cadastro
    cli = db.execute("SELECT tipo_veiculo FROM CLIENTES WHERE placa = ?", (placa,)).fetchone()
    if cli and cli['tipo_veiculo']: return cli['tipo_veiculo']
    # 2. Checa histórico de tickets pagos
    tkt = db.execute("SELECT tipo FROM TICKETS WHERE placa = ? AND status = 'PAGO' ORDER BY hora_saida DESC LIMIT 1", (placa,)).fetchone()
    if tkt and tkt['tipo'] in ['CARRO', 'MOTO']: return tkt['tipo']
    return None

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
    if not value: return datetime.min
    try: return datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
    except: return datetime.min

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
            db.execute("UPDATE CAIXA SET data_fechamento=?, saldo_final=?, status='FECHADO_AUTO' WHERE id=?", (agora, caixa['saldo_inicial']+tot, caixa['id']))
            
            if tot > 0:
                obs = f"Fechamento Auto (Virada de Dia) Caixa #{caixa['id']}"
                db.execute("INSERT INTO RECEITAS (descricao, valor, data_vencimento, data_recebimento, categoria, recorrente, observacao, status) VALUES (?,?,?,?,?,?,?,?)", 
                           (f"Fechamento Auto Caixa #{caixa['id']}", tot, obter_data_br(), obter_data_br(), 'Fechamento de Caixa', 0, obs, 'RECEBIDO'))
            
            db.execute("INSERT INTO CAIXA (data_abertura, saldo_inicial, usuario_abertura_id, status) VALUES (?, ?, ?, ?)", (agora, caixa['saldo_inicial'], session['user_id'], 'ABERTO'))
            db.commit(); flash('Novo caixa aberto (Virada de dia).', 'warning')
        return f(*args, **kwargs)
    return decorated_function

# --- BANCO DE DADOS ---
def init_db():
    with app.app_context():
        db = get_db()
        db.execute("CREATE TABLE IF NOT EXISTS TICKETS (id INTEGER PRIMARY KEY, placa TEXT NOT NULL, tipo TEXT DEFAULT 'CARRO', local_vaga TEXT, numero_sequencial INTEGER, hora_entrada TEXT NOT NULL, hora_saida TEXT, valor_total REAL, status TEXT DEFAULT 'ESTACIONADO', caixa_id INTEGER, forma_pagamento TEXT DEFAULT 'DINHEIRO')")
        db.execute("CREATE TABLE IF NOT EXISTS CLIENTES (id INTEGER PRIMARY KEY AUTOINCREMENT, nome TEXT NOT NULL, telefone TEXT, placa TEXT NOT NULL UNIQUE)")
        db.execute("CREATE TABLE IF NOT EXISTS ESTABELECIMENTO (id INTEGER PRIMARY KEY, nome TEXT, cnpj TEXT, endereco TEXT, telefone TEXT, total_vagas INTEGER DEFAULT 50)")
        db.execute("CREATE TABLE IF NOT EXISTS TARIFAS (id INTEGER PRIMARY KEY, valor_carro REAL, valor_moto REAL, teto_diaria REAL, tolerancia_minutos INTEGER)")
        db.execute("CREATE TABLE IF NOT EXISTS USUARIOS (id INTEGER PRIMARY KEY AUTOINCREMENT, nome TEXT NOT NULL, username TEXT NOT NULL UNIQUE, senha TEXT NOT NULL, perfil TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS CAIXA (id INTEGER PRIMARY KEY AUTOINCREMENT, data_abertura TEXT NOT NULL, data_fechamento TEXT, saldo_inicial REAL DEFAULT 0, saldo_final REAL, troco_deixado REAL DEFAULT 0, usuario_abertura_id INTEGER, status TEXT DEFAULT 'ABERTO')")
        db.execute("CREATE TABLE IF NOT EXISTS FORMAS_PAGAMENTO (id INTEGER PRIMARY KEY AUTOINCREMENT, nome TEXT NOT NULL UNIQUE, ativo INTEGER DEFAULT 1)")
        db.execute("CREATE TABLE IF NOT EXISTS DESPESAS (id INTEGER PRIMARY KEY AUTOINCREMENT, descricao TEXT, valor REAL, data_vencimento TEXT, data_pagamento TEXT, categoria TEXT, recorrente INTEGER, observacao TEXT, status TEXT DEFAULT 'PENDENTE')")
        db.execute("CREATE TABLE IF NOT EXISTS RECEITAS (id INTEGER PRIMARY KEY AUTOINCREMENT, descricao TEXT, valor REAL, data_vencimento TEXT, data_recebimento TEXT, categoria TEXT, recorrente INTEGER, observacao TEXT, status TEXT DEFAULT 'PENDENTE')")

        def check_and_add(table, col, type_def):
            try:
                cols = [i[1] for i in db.execute(f"PRAGMA table_info({table})").fetchall()]
                if col not in cols: db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {type_def}")
            except: pass

        for c in ['is_whatsapp','marca_veiculo','modelo_veiculo','cor_veiculo','observacoes','tipo_veiculo','tipo_cliente','plano_mensal','regra_inicio','data_inicio_ciclo','data_fim_ciclo']: check_and_add('CLIENTES', c, 'TEXT')
        for c in ['is_eletrico','is_suv']: check_and_add('CLIENTES', c, 'INTEGER DEFAULT 0')
        for c in ['mensal_diurno','mensal_noturno','mensal_integral']: check_and_add('TARIFAS', c, 'REAL DEFAULT 0')
        check_and_add('TICKETS', 'numero_sequencial', 'INTEGER'); check_and_add('TICKETS', 'local_vaga', 'TEXT')
        check_and_add('USUARIOS', 'ultimo_acesso', 'TEXT')

        if db.execute("SELECT COUNT(*) FROM FORMAS_PAGAMENTO").fetchone()[0] == 0:
            for f in ['Dinheiro', 'Pix', 'Cartão de Débito', 'Cartão de Crédito']: db.execute("INSERT INTO FORMAS_PAGAMENTO (nome) VALUES (?)", (f,))
        if db.execute("SELECT COUNT(*) FROM ESTABELECIMENTO").fetchone()[0] == 0:
            db.execute("INSERT INTO ESTABELECIMENTO (nome, total_vagas) VALUES (?, ?)", ("ParkSystem Pro", 50))
        if db.execute("SELECT COUNT(*) FROM TARIFAS").fetchone()[0] == 0: 
            db.execute("INSERT INTO TARIFAS (valor_carro, valor_moto, teto_diaria, tolerancia_minutos, mensal_diurno, mensal_noturno, mensal_integral) VALUES (10, 5, 50, 15, 150, 120, 250)")
        if db.execute("SELECT COUNT(*) FROM USUARIOS").fetchone()[0] == 0:
            db.execute("INSERT INTO USUARIOS (nome, username, senha, perfil) VALUES (?, ?, ?, ?)", ('Super Admin', 'admin', generate_password_hash('admin', method='pbkdf2:sha256'), 'ADMIN'))
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
                    if datetime.strptime(cli['data_fim_ciclo'].split(' ')[0], '%Y-%m-%d').date() >= obter_data_br(): return 0, 0.00
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
        u = get_db().execute("SELECT * FROM USUARIOS WHERE username = ?", (request.form['username'],)).fetchone()
        if u and check_password_hash(u['senha'], request.form['senha']):
            session['user_id'] = u['id']; session['user_nome'] = u['nome']; session['user_perfil'] = u['perfil']
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
                       (f"Fechamento Caixa #{cx['id']}", tot, obter_data_br(), obter_data_br(), 'Fechamento de Caixa', 0, obs, 'RECEBIDO'))
        
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

# [MODIFICADO] Entrada (Auditoria)
@app.route('/entrada', methods=['GET','POST'])
@login_required
@caixa_required
def dar_entrada():
    if request.method=='POST':
        p=request.form['placa'].upper().replace('-','').strip(); t=request.form.get('tipo','CARRO'); l=request.form.get('local_vaga','').upper()
        db=get_db(); cli=db.execute("SELECT * FROM CLIENTES WHERE placa=?",(p,)).fetchone()
        if db.execute("SELECT 1 FROM TICKETS WHERE placa=? AND status='ESTACIONADO'",(p,)).fetchone(): flash('Já no pátio','warning'); return redirect(url_for('dar_entrada'))
        
        # Tipo regra: se mensalista, usa MENSALISTA. Se avulso, usa o tipo informado (t)
        tipo_reg = cli['tipo_cliente'] if cli and cli['tipo_cliente'] == 'MENSALISTA' else t
        
        if cli and cli['tipo_cliente']=='MENSALISTA':
            hj=obter_data_br()
            if cli['data_fim_ciclo'] and datetime.strptime(cli['data_fim_ciclo'].split(' ')[0],'%Y-%m-%d').date() < hj:
                flash('Mensalidade Vencida! Cobrando avulso.','danger'); tipo_reg = t 
            elif cli['regra_inicio']=='PRIMEIRO_USO' and not cli['data_inicio_ciclo']:
                db.execute("UPDATE CLIENTES SET data_inicio_ciclo=?, data_fim_ciclo=? WHERE id=?",(obter_data_br(), obter_data_br()+timedelta(days=30), cli['id'])).connection.commit(); flash('Mensalista ativado!','success')
        
        # GRAVA O TIPO ESCOLHIDO (t) se não for mensalista, para auditar
        final_type = t if not (cli and cli['tipo_cliente'] == 'MENSALISTA') else tipo_reg
        
        ns = None if tipo_reg=='MENSALISTA' else 0
        db.execute("INSERT INTO TICKETS (placa,hora_entrada,status,tipo,local_vaga,numero_sequencial) VALUES (?,?,?,?,?,?)",(p,obter_hora_br().strftime('%Y-%m-%d %H:%M:%S'),'ESTACIONADO',final_type,l,ns)).connection.commit()
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

# --- FINANCEIRO ---
@app.route('/financeiro', methods=['GET','POST'])
@login_required
@admin_required
def financeiro():
    db = get_db()
    
    hoje = obter_data_br(); primeiro_dia = hoje.replace(day=1)
    passado_distante = (hoje - timedelta(days=365)).strftime('%Y-%m-%d')
    
    active_tab = request.args.get('tab', request.values.get('active_tab', 'mensal'))
    
    # Filtros Vendas (Default: HOJE)
    v_ini_form = request.values.get('vendas_ini'); v_fim_form = request.values.get('vendas_fim')
    v_ini = v_ini_form if v_ini_form else hoje.strftime('%Y-%m-%d'); v_fim = v_fim_form if v_fim_form else hoje.strftime('%Y-%m-%d')

    # Filtros Receitas (Default: Mês Atual até 2100)
    car_ini_form = request.values.get('car_ini'); car_fim_form = request.values.get('car_fim'); car_status = request.values.get('car_status', 'TODOS')
    car_ini = car_ini_form if car_ini_form else primeiro_dia.strftime('%Y-%m-%d'); car_fim = car_fim_form if car_fim_form else MAX_SQL_DATE
    
    # Filtros Despesas (Default: Mês Atual até 2100)
    cap_ini_form = request.values.get('cap_ini'); cap_fim_form = request.values.get('cap_fim'); cap_status = request.values.get('cap_status', 'TODOS')
    cap_ini = cap_ini_form if cap_ini_form else primeiro_dia.strftime('%Y-%m-%d'); cap_fim = cap_fim_form if cap_fim_form else MAX_SQL_DATE
    
    # Filtros Caixas (Default: 1 Ano Atrás até Hoje)
    c_ini_form = request.values.get('caixa_ini'); c_fim_form = request.values.get('caixa_fim'); c_op = request.values.get('caixa_op', 'TODOS')
    c_ini = c_ini_form if c_ini_form else passado_distante; c_fim = c_fim_form if c_fim_form else hoje.strftime('%Y-%m-%d')

    # --- CONSULTAS ---
    
    # 1. Despesas
    q_desp = "SELECT * FROM DESPESAS WHERE data_vencimento BETWEEN ? AND ?"; p_desp = [cap_ini, cap_fim]
    if cap_status != 'TODOS': q_desp += " AND status = ?"; p_desp.append(cap_status)
    despesas = db.execute(q_desp+" ORDER BY data_vencimento", p_desp).fetchall()
    
    # 2. Receitas
    q_rec = "SELECT * FROM RECEITAS WHERE data_vencimento BETWEEN ? AND ?"; p_rec = [car_ini, car_fim]
    if car_status != 'TODOS':
        s = 'RECEBIDO' if car_status == 'PAGO' else 'PENDENTE'
        q_rec += " AND status = ?"; p_rec.append(s)
    receitas = db.execute(q_rec+" ORDER BY data_vencimento", p_rec).fetchall()
    
    # 3. Mensalistas
    mensalistas = db.execute("SELECT * FROM CLIENTES WHERE tipo_cliente='MENSALISTA' ORDER BY data_fim_ciclo").fetchall()

    # 4. Caixas
    q_cx = "SELECT C.*,U.nome as nome_operador,(SELECT SUM(valor_total) FROM TICKETS WHERE caixa_id=C.id AND status='PAGO') as vendas FROM CAIXA C LEFT JOIN USUARIOS U ON C.usuario_abertura_id=U.id WHERE date(C.data_abertura) BETWEEN ? AND ?"; p_cx = [c_ini, c_fim]
    if c_op!='TODOS': q_cx+=" AND C.usuario_abertura_id=?"; p_cx.append(c_op)
    q_cx+=" ORDER BY C.id DESC"; cx_raw = db.execute(q_cx, p_cx).fetchall()
    caixas = []; tot_v_cx=0; tot_f_cx=0
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
    tkt_mes = db.execute("SELECT SUM(valor_total) FROM TICKETS WHERE status='PAGO' AND date(hora_saida)>=?", (ini_mes_str,)).fetchone()[0] or 0.0
    rec_mes = db.execute("SELECT SUM(valor) FROM RECEITAS WHERE status='RECEBIDO' AND categoria != 'Fechamento de Caixa' AND date(data_recebimento)>=?", (ini_mes_str,)).fetchone()[0] or 0.0
    entradas_mes = tkt_mes + rec_mes
    saidas_mes = db.execute("SELECT SUM(valor) FROM DESPESAS WHERE status='PAGO' AND date(data_pagamento) >= ?", (ini_mes_str,)).fetchone()[0] or 0.0

    sum_tkt_filtered = sum(t['valor_total'] for t in raw_tickets)
    total_entradas = sum_tkt_filtered + sum_receitas
    saldo_periodo = total_entradas - sum_despesas 

    return render_template('financeiro.html', 
                           mensalistas=mensalistas, despesas=despesas, receitas=receitas, caixas=caixas, tickets_agrupados=tickets_agrupados,
                           total_entradas=total_entradas, total_saidas=sum_despesas, saldo_periodo=saldo_periodo,
                           entradas_hoje=ent_hj, entradas_mes=entradas_mes, saidas_mes=saidas_mes,
                           
                           # Filtros Visuais
                           vendas_ini=v_ini_form, vendas_fim=v_fim_form,
                           car_ini=car_ini_form, car_fim=car_fim_form, car_status=car_status,
                           cap_ini=cap_ini_form, cap_fim=cap_fim_form, cap_status=cap_status,
                           caixa_ini=c_ini_form, caixa_fim=c_fim_form, caixa_op=c_op,
                           
                           total_vendas_caixa=tot_v_cx, total_acumulado_caixa=tot_f_cx,
                           active_tab=active_tab,
                           tarifas=db.execute("SELECT * FROM TARIFAS LIMIT 1").fetchone(), 
                           formas=db.execute("SELECT * FROM FORMAS_PAGAMENTO WHERE ativo=1").fetchall(), 
                           usuarios=db.execute("SELECT * FROM USUARIOS").fetchall())

@app.route('/financeiro/receber_mensalidade/<int:id>', methods=['POST'])
@login_required
def receber_mensalidade_financeiro(id):
    db=get_db(); cx=db.execute("SELECT id FROM CAIXA WHERE status='ABERTO'").fetchone()
    if not cx: flash('Abra o caixa.','danger'); return redirect(url_for('financeiro', tab='mensal'))
    cli=db.execute("SELECT * FROM CLIENTES WHERE id=?",(id,)).fetchone()
    hj = obter_data_br(); dt_fim = datetime.strptime(cli['data_fim_ciclo'], '%Y-%m-%d').date() if cli['data_fim_ciclo'] else hj
    nv = (hj if dt_fim < hj else dt_fim) + timedelta(days=30)
    db.execute("UPDATE CLIENTES SET data_fim_ciclo=? WHERE id=?",(nv,id))
    db.execute("INSERT INTO TICKETS (placa,tipo,hora_entrada,hora_saida,valor_total,status,caixa_id,forma_pagamento) VALUES (?,'MENSALIDADE',?,?,?,'PAGO',?,?)", (cli['placa'],obter_hora_br(),obter_hora_br(),safe_float(request.form['valor']),cx['id'],request.form['forma_pagamento']))
    db.commit(); return redirect(url_for('financeiro', status='PENDENTE', tab='mensal'))

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

@app.route('/financeiro/despesa/pagar/<int:id>')
@login_required
def pagar_despesa(id): get_db().execute("UPDATE DESPESAS SET status='PAGO', data_pagamento=? WHERE id=?",(obter_data_br(),id)).connection.commit(); return redirect(url_for('financeiro', status='PENDENTE', tab='cap'))

@app.route('/financeiro/despesa/excluir/<int:id>/<string:modo>')
@login_required
def excluir_despesa(id, modo):
    db = get_db()
    if modo == 'unico': db.execute("DELETE FROM DESPESAS WHERE id=?", (id,))
    elif modo == 'todos':
        alvo = db.execute("SELECT descricao, categoria FROM DESPESAS WHERE id=?", (id,)).fetchone()
        if alvo:
            raiz = re.sub(r' \(\d+/\d+\)$', '', alvo['descricao'])
            db.execute("DELETE FROM DESPESAS WHERE id >= ? AND categoria = ? AND status = 'PENDENTE' AND (descricao = ? OR descricao LIKE ?)", (id, alvo['categoria'], raiz, f"{raiz} (%"))
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
            if recorrencia == 'MENSAL': n_data = add_months(d_base, i)
            elif recorrencia == 'SEMANAL': n_data = d_base + timedelta(weeks=i)
            elif recorrencia == 'QUINZENAL': n_data = d_base + timedelta(weeks=2 * i)
            elif recorrencia == 'DIARIA': n_data = d_base + timedelta(days=i)
            elif recorrencia == 'ANUAL': n_data = add_months(d_base, i*12)
            get_db().execute("INSERT INTO RECEITAS (descricao, valor, data_vencimento, categoria, recorrente, observacao, status) VALUES (?, ?, ?, ?, ?, ?, ?)", (f"{desc} ({i+1}/{qtd_total})", val, n_data.strftime('%Y-%m-%d'), cat, 1, obs, 'PENDENTE')).connection.commit()
    return redirect(url_for('financeiro', status='PENDENTE', tab='car'))

@app.route('/financeiro/receita/receber/<int:id>')
@login_required
def receber_receita(id): get_db().execute("UPDATE RECEITAS SET status='RECEBIDO', data_recebimento=? WHERE id=?",(obter_data_br(),id)).connection.commit(); return redirect(url_for('financeiro', status='PENDENTE', tab='car'))

@app.route('/financeiro/receita/excluir/<int:id>/<string:modo>')
@login_required
def excluir_receita(id, modo):
    db = get_db()
    if modo == 'unico': db.execute("DELETE FROM RECEITAS WHERE id=?", (id,))
    elif modo == 'todos':
        alvo = db.execute("SELECT descricao, categoria FROM RECEITAS WHERE id=?", (id,)).fetchone()
        if alvo:
            raiz = re.sub(r' \(\d+/\d+\)$', '', alvo['descricao'])
            db.execute("DELETE FROM RECEITAS WHERE id >= ? AND categoria = ? AND status = 'PENDENTE' AND (descricao = ? OR descricao LIKE ?)", (id, alvo['categoria'], raiz, f"{raiz} (%"))
    db.commit(); return redirect(url_for('financeiro', tab='car'))

@app.route('/financeiro/receita/editar/<int:id>', methods=['POST'])
@login_required
def editar_receita(id):
    get_db().execute("UPDATE RECEITAS SET descricao=?, valor=?, data_vencimento=?, categoria=?, observacao=? WHERE id=?", (request.form['descricao'],safe_float(request.form['valor']),request.form['vencimento'],request.form['categoria'],request.form['observacao'],id)).connection.commit()
    return redirect(url_for('financeiro', tab='car'))

# [MODIFICADO] Relatórios com Filtros 4 Campos e Auditoria
@app.route('/relatorios', methods=['GET', 'POST'])
@login_required
def relatorios():
    db = get_db()
    
    hoje_date = datetime.now(BR_TZ).strftime('%Y-%m-%d')
    
    dt_ini_form = request.values.get('data_inicio'); hora_ini_form = request.values.get('hora_inicio')
    dt_fim_form = request.values.get('data_fim'); hora_fim_form = request.values.get('hora_fim')
    termo = request.values.get('termo', '').strip()
    tipo_filtro = request.values.get('tipo_cliente', 'TODOS')
    pgto_filtro = request.values.get('forma_pagamento', 'TODOS')

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
    if termo: q = q.replace("WHERE", "WHERE (T.placa LIKE ? OR C.nome LIKE ? OR T.numero_sequencial = ?) AND"); params = [f"%{termo}%", f"%{termo}%", int(re.sub(r'[^0-9]','',termo) or -1)] + params

    if tipo_filtro == 'MENSALISTA': q += " AND T.tipo = 'MENSALISTA'"
    elif tipo_filtro == 'AVULSO': q += " AND T.tipo != 'MENSALISTA'"
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
        
        # --- LÓGICA DE AUDITORIA CORRIGIDA ---
        divergencia = False
        
        # 1. Pega o tipo que deveria ser (Cadastro ou Histórico)
        tipo_esperado = t['tipo_cadastrado']
        
        # Se não tem cadastro, busca o histórico de tickets ANTERIORES a este ticket específico
        # para saber o que era considerado "padrão" para este veículo antes desta venda
        if not tipo_esperado:
             # Busca o último ticket pago DESTA PLACA que não seja o atual
             hist_tkt = db.execute("SELECT tipo FROM TICKETS WHERE placa = ? AND status='PAGO' AND id != ? ORDER BY hora_saida DESC LIMIT 1", (t['placa'], t['id'])).fetchone()
             if hist_tkt:
                 tipo_esperado = hist_tkt['tipo']
        
        # 2. Compara: Se existe uma expectativa E o ticket atual é diferente
        if tipo_esperado and t['tipo'] != 'MENSALIDADE':
             if str(t['tipo']).upper() != str(tipo_esperado).upper():
                 if t['tipo_cliente'] != 'MENSALISTA': # Ignora mensalistas
                     divergencia = True

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
             'tipo_cadastrado': tipo_esperado or 'N/A'} # Passa o tipo esperado para o tooltip
        
        k = fmt_placa(t['placa'])
        dados_agrupados.setdefault(k, {'placa': k, 'nome_cliente': d['nome_cliente'], 'tipo_cliente': t['tipo_cliente'], 'total_pago_periodo': 0.0, 'tickets': []})
        dados_agrupados[k]['tickets'].append(d); dados_agrupados[k]['total_pago_periodo'] += t['valor_total']

    return render_template('relatorios.html', dados_agrupados=dados_agrupados, total_geral=tot_geral, totais_pgto=totais_pgto, filtro_inicio_date=dt_ini_form, filtro_hora_inicio=hora_ini_form, filtro_fim_date=dt_fim_form, filtro_hora_fim=hora_fim_form, filtro_termo=termo, filtro_tipo=tipo_filtro, filtro_pgto=pgto_filtro, formas_pagamento=db.execute("SELECT * FROM FORMAS_PAGAMENTO WHERE ativo=1").fetchall())

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
    raw=db.execute("SELECT T.*,C.nome as nome_cliente,C.id as cid,C.tipo_cliente,C.tipo_veiculo as tipo_cadastrado FROM TICKETS T LEFT JOIN CLIENTES C ON T.placa=C.placa WHERE T.status='PAGO' AND T.caixa_id=? ORDER BY T.hora_saida DESC",(id,)).fetchall()
    
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
    return render_template('recibo_impressao.html', valor=t['valor_total'], placa=fmt_placa(t['placa']), entrada=fmt_data(t['hora_entrada']), saida=fmt_data(t['hora_saida']), ticket_numero=gerar_codigo_visual(t['numero_sequencial'], t['cid'], t['tipo_cliente']), estab=estab)

@app.route('/configuracoes', methods=['GET','POST'])
@login_required
@admin_required
def configuracoes():
    db=get_db()
    if request.method=='POST':
        if 'add_pagamento' in request.form: db.execute("INSERT INTO FORMAS_PAGAMENTO (nome) VALUES (?)",(request.form['nova_forma'],)); db.commit()
        elif 'del_pagamento' in request.form: db.execute("DELETE FROM FORMAS_PAGAMENTO WHERE id=?",(request.form['id_forma'],)); db.commit()
        elif 'save_tarifas' in request.form: 
            db.execute("UPDATE TARIFAS SET valor_carro=?, valor_moto=?, teto_diaria=?, tolerancia_minutos=?, mensal_diurno=?, mensal_noturno=?, mensal_integral=? WHERE id=1", (safe_float(request.form['valor_carro']), safe_float(request.form['valor_moto']), safe_float(request.form['teto_diaria']), int(request.form['tolerancia_minutos']), safe_float(request.form['mensal_diurno']), safe_float(request.form['mensal_noturno']), safe_float(request.form['mensal_integral']))); db.commit()
        elif 'save_empresa' in request.form:
            db.execute("UPDATE ESTABELECIMENTO SET nome=?, cnpj=?, endereco=?, telefone=?, total_vagas=? WHERE id=1", (request.form['estab_nome'], request.form['estab_cnpj'], request.form['estab_endereco'], request.form['estab_telefone'], int(request.form['estab_vagas']))); db.commit()
        return redirect(url_for('configuracoes'))
    return render_template('configuracoes.html', conf=db.execute("SELECT * FROM TARIFAS").fetchone(), estab=db.execute("SELECT * FROM ESTABELECIMENTO").fetchone(), formas=db.execute("SELECT * FROM FORMAS_PAGAMENTO").fetchall())

@app.route('/clientes')
@login_required
def listar_clientes(): return render_template('listar_clientes.html', clientes=get_db().execute("SELECT * FROM CLIENTES ORDER BY nome").fetchall())

@app.route('/clientes/novo', methods=['GET','POST'])
@login_required
def novo_cliente():
    if request.method=='POST':
        dt_ini, dt_fim = None, None
        if request.form['tipo_cliente']=='MENSALISTA' and request.form.get('regra_inicio')=='IMEDIATO': dt_ini=obter_data_br(); dt_fim=dt_ini+timedelta(days=30)
        try: get_db().execute("INSERT INTO CLIENTES (nome,telefone,is_whatsapp,placa,marca_veiculo,modelo_veiculo,cor_veiculo,observacoes,is_eletrico,is_suv,tipo_veiculo,tipo_cliente,plano_mensal,regra_inicio,data_inicio_ciclo,data_fim_ciclo) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (request.form['nome'], request.form['telefone'], 1 if request.form.get('is_whatsapp') else 0, request.form['placa'].upper().replace('-','').strip(), request.form.get('marca_veiculo'), request.form.get('modelo_veiculo'), request.form.get('cor_veiculo'), request.form.get('observacoes'), 1 if request.form.get('is_eletrico') else 0, 1 if request.form.get('is_suv') else 0, request.form.get('tipo_veiculo'), request.form['tipo_cliente'], request.form.get('plano_mensal'), request.form.get('regra_inicio'), dt_ini, dt_fim)).connection.commit(); return redirect(url_for('listar_clientes'))
        except: flash('Placa já existe','danger')
    return render_template('form_cliente.html', cliente=None)

@app.route('/clientes/editar/<int:id>', methods=['GET','POST'])
@login_required
def editar_cliente(id):
    db=get_db(); c=db.execute("SELECT * FROM CLIENTES WHERE id=?",(id,)).fetchone()
    if request.method=='POST':
        db.execute("UPDATE CLIENTES SET nome=?, telefone=?, is_whatsapp=?, placa=?, marca_veiculo=?, modelo_veiculo=?, cor_veiculo=?, observacoes=?, is_eletrico=?, is_suv=?, tipo_veiculo=?, tipo_cliente=?, plano_mensal=? WHERE id=?", (request.form['nome'], request.form['telefone'], 1 if request.form.get('is_whatsapp') else 0, request.form['placa'].upper().replace('-','').strip(), request.form.get('marca_veiculo'), request.form.get('modelo_veiculo'), request.form.get('cor_veiculo'), request.form.get('observacoes'), 1 if request.form.get('is_eletrico') else 0, 1 if request.form.get('is_suv') else 0, request.form.get('tipo_veiculo'), request.form['tipo_cliente'], request.form.get('plano_mensal'), id)).connection.commit(); return redirect(url_for('listar_clientes'))
    return render_template('form_cliente.html', cliente=c)

@app.route('/clientes/excluir/<int:id>')
@login_required
def excluir_cliente(id): get_db().execute("DELETE FROM CLIENTES WHERE id=?",(id,)).connection.commit(); return redirect(url_for('listar_clientes'))

@app.route('/usuarios')
@login_required
@admin_required
def listar_usuarios(): return render_template('listar_usuarios.html', usuarios=get_db().execute("SELECT * FROM USUARIOS").fetchall())

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

@app.route('/perfil', methods=['GET','POST'])
@login_required
def perfil():
    if request.method=='POST':
        u=get_db().execute("SELECT * FROM USUARIOS WHERE id=?",(session['user_id'],)).fetchone()
        if check_password_hash(u['senha'],request.form['senha_atual']) and request.form['nova_senha']==request.form['confirmar_senha']: get_db().execute("UPDATE USUARIOS SET senha=? WHERE id=?",(generate_password_hash(request.form['nova_senha'],method='pbkdf2:sha256'),session['user_id'])).connection.commit(); flash('Senha alterada!','success')
        else: flash('Erro!','danger')
    return render_template('perfil.html')

if __name__ == '__main__':
    init_db()
    app.run(debug=True, port=5000)