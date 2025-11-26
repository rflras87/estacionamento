import sqlite3
import time
# ### NOVO ### Adicionado 'import pytz' para lidar com fusos horários
from datetime import datetime
import pytz 
from flask import Flask, render_template, request, g, redirect, url_for, flash
import re 
import math

# --- 1. Configuração Inicial ---
app = Flask(__name__)
app.config['SECRET_KEY'] = 'uma_chave_secreta_muito_segura' 
DATABASE = 'estacionamento.db' 

# ### NOVO ### Definição do Fuso Horário Brasileiro
# Isso garante que o servidor na nuvem (que usa UTC) saiba que estamos no Brasil.
BR_TZ = pytz.timezone('America/Sao_Paulo')

# ### NOVO ### Função Ajudante para pegar a hora certa
def obter_hora_br():
    """Retorna a data e hora atual no fuso de SP, pronta para cálculos."""
    # Pega a hora atual no fuso correto e remove a informação de fuso (tzinfo=None)
    # para facilitar os cálculos de subtração com as datas salvas no banco.
    return datetime.now(BR_TZ).replace(tzinfo=None)


# --- 2. Funções de Conexão com o Banco de Dados ---

def get_db():
    """Obtém a conexão com o banco de dados. Cria se não existir."""
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row 
    return db

@app.teardown_appcontext
def close_connection(exception):
    """Fecha a conexão com o DB ao final da requisição."""
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

# --- 3. Inicialização do DB e Definição de Preços ---

def init_db():
    """Cria a estrutura inicial do banco de dados (TICKETS e TARIFAS)."""
    with app.app_context():
        db = get_db()
        
        db.execute("""
            CREATE TABLE IF NOT EXISTS TICKETS (
                id INTEGER PRIMARY KEY,
                placa TEXT NOT NULL,
                hora_entrada TEXT NOT NULL,
                hora_saida TEXT,
                valor_total REAL,
                status TEXT NOT NULL DEFAULT 'ESTACIONADO'
            );
        """)
        
        db.execute("""
            CREATE TABLE IF NOT EXISTS TARIFAS (
                id INTEGER PRIMARY KEY,
                valor_hora REAL NOT NULL
            );
        """)
        
        # Insere R$ 10.00 como valor padrão se a tabela estiver vazia
        cursor = db.execute("SELECT COUNT(*) FROM TARIFAS")
        if cursor.fetchone()[0] == 0:
            db.execute("INSERT INTO TARIFAS (valor_hora) VALUES (?)", (10.00,))

        db.commit()

# --- 4. Funções de Lógica de Negócio (Cálculo e Formatação) ---

def calcular_tempo_e_valor(hora_entrada_str, hora_saida_str=None):
    """Calcula a diferença de tempo e o valor a pagar."""
    
    if hora_saida_str is None:
        # ### ALTERADO ### Usa a nossa função BR em vez de datetime.now()
        hora_saida_dt = obter_hora_br()
    else:
        try:
            hora_saida_dt = datetime.strptime(hora_saida_str, '%Y-%m-%d %H:%M:%S')
        except ValueError:
            return 1, 0.0

    try:
        hora_entrada_dt = datetime.strptime(hora_entrada_str, '%Y-%m-%d %H:%M:%S')
    except ValueError:
        return 1, 0.0

    db = get_db()
    tarifa = db.execute("SELECT valor_hora FROM TARIFAS LIMIT 1").fetchone()
    valor_hora = tarifa['valor_hora'] if tarifa else 10.00

    diferenca = hora_saida_dt - hora_entrada_dt
    total_segundos = diferenca.total_seconds()
    
    if total_segundos <= 0:
        horas_cobradas = 1
    else:
        total_horas = (total_segundos / 3600) 
        horas_cobradas = math.ceil(total_horas)
    
    valor_total = horas_cobradas * valor_hora
    
    valor_total = min(valor_total, 50.00) # Teto Máximo
    
    return horas_cobradas, valor_total

def formatar_placa(placa):
    """Formata a placa para o padrão visual (com traço)."""
    if len(placa) == 7:
        return f"{placa[:3]}-{placa[3:]}"
    return placa

def formatar_datahora(datahora_str):
    """Formata a string do banco de dados para o padrão brasileiro (DD/MM/AAAA HH:MM:SS)."""
    if not datahora_str:
        return "N/A"
    try:
        dt_obj = datetime.strptime(datahora_str, '%Y-%m-%d %H:%M:%S')
        return dt_obj.strftime('%d/%m/%Y %H:%M:%S')
    except:
        return datahora_str

def formatar_ticket_id(id):
    """Formata o ID do ticket com zeros à esquerda (TCK-XXXXXX)."""
    return f"TCK-{str(id).zfill(6)}"

# --- ROTAS DO FLASK ---

@app.route('/')
def index():
    """Rota raiz que redireciona para a nova página Home."""
    return redirect(url_for('home'))

@app.route('/home')
def home():
    """Página inicial/Home do sistema."""
    return render_template('home.html')

@app.route('/entrada', methods=['GET', 'POST'])
def dar_entrada():
    """Página de entrada do veículo."""
    if request.method == 'POST':
        placa = request.form.get('placa', '').upper().replace('-', '').strip()
        
        if not placa:
            flash('A placa é obrigatória.', 'danger')
            return redirect(url_for('dar_entrada'))
            
        db = get_db()
        
        # Validação de formato (7 caracteres alfanuméricos)
        if len(placa) != 7 or not placa.isalnum():
             flash('Placa inválida. Use 7 caracteres alfanuméricos.', 'danger')
             return redirect(url_for('dar_entrada'))
            
        # 1. Verifica se a placa já está estacionada
        existente = db.execute(
            "SELECT * FROM TICKETS WHERE placa = ? AND status = 'ESTACIONADO'", 
            (placa,)
        ).fetchone()
        
        if existente:
            flash(f'O veículo de placa {formatar_placa(placa)} já está estacionado (Ticket N° {formatar_ticket_id(existente["id"])}).', 'warning')
            return redirect(url_for('dar_entrada'))
            
        # 2. Cria o novo ticket
        # ### ALTERADO ### Usa a nossa função BR para gravar a entrada
        hora_entrada = obter_hora_br().strftime('%Y-%m-%d %H:%M:%S')
        
        db.execute(
            "INSERT INTO TICKETS (placa, hora_entrada, status) VALUES (?, ?, ?)", 
            (placa, hora_entrada, 'ESTACIONADO')
        )
        db.commit()
        
        # Busca o ID do ticket recém-criado
        novo_ticket_id = db.execute("SELECT id FROM TICKETS WHERE placa = ? ORDER BY id DESC LIMIT 1", (placa,)).fetchone()[0]
        
        flash(f'Entrada registrada com sucesso! Ticket N° {formatar_ticket_id(novo_ticket_id)}.', 'success')
        return redirect(url_for('listar_estacionados'))
        
    return render_template('form_entrada.html') 

@app.route('/estacionados')
def listar_estacionados():
    """Lista todos os veículos atualmente estacionados."""
    db = get_db()
    tickets = db.execute(
        "SELECT * FROM TICKETS WHERE status = 'ESTACIONADO' ORDER BY hora_entrada DESC"
    ).fetchall()
    
    lista_estacionados = []
    for ticket in tickets:
        horas_cobradas, valor_total = calcular_tempo_e_valor(ticket['hora_entrada'])
        
        lista_estacionados.append({
            'id': ticket['id'],
            'ticket_numero': formatar_ticket_id(ticket['id']),
            'placa': formatar_placa(ticket['placa']),
            'entrada': formatar_datahora(ticket['hora_entrada']),
            'horas_cobradas': horas_cobradas,
            'valor_a_pagar': valor_total,
            'tempo_em_horas': horas_cobradas
        })
        
    return render_template('listar_estacionados.html', tickets=lista_estacionados)

# --- ROTA 1 de 2: VISUALIZAÇÃO E CONFIRMAÇÃO ---
@app.route('/saida/<int:ticket_id>')
def visualizar_pagamento(ticket_id):
    """Calcula o valor e exibe a tela de confirmação antes de atualizar o DB."""
    db = get_db()
    ticket = db.execute(
        "SELECT * FROM TICKETS WHERE id = ? AND status = 'ESTACIONADO'", 
        (ticket_id,)
    ).fetchone()

    if not ticket:
        flash("Ticket não encontrado ou já pago.", 'danger')
        return redirect(url_for('listar_estacionados'))
        
    hora_entrada_str = ticket['hora_entrada']
    
    _, valor_total = calcular_tempo_e_valor(hora_entrada_str)
    
    placa_visual = formatar_placa(ticket['placa'])
    entrada_br = formatar_datahora(hora_entrada_str)
    
    # ### ALTERADO ### Usa a nossa função BR para mostrar a saída prevista
    saida_br = obter_hora_br().strftime('%d/%m/%Y %H:%M:%S') 
    ticket_numero = formatar_ticket_id(ticket_id)
    
    return render_template('confirmacao_pagamento.html', 
                           ticket_id=ticket_id,
                           valor=valor_total, 
                           placa=placa_visual,
                           entrada=entrada_br, 
                           saida=saida_br,
                           ticket_numero=ticket_numero)

# --- ROTA 2 de 2: FINALIZAÇÃO E PAGAMENTO (POST) ---
@app.route('/pagar/<int:ticket_id>', methods=['POST'])
def finalizar_pagamento(ticket_id):
    """Finaliza a transação, registra a saída no DB e exibe o recibo."""
    db = get_db()
    
    ticket = db.execute(
        "SELECT placa, hora_entrada FROM TICKETS WHERE id = ? AND status = 'ESTACIONADO'", 
        (ticket_id,)
    ).fetchone()

    if not ticket:
        flash("Erro de transação: Ticket não encontrado ou já pago.", 'danger')
        return redirect(url_for('listar_estacionados'))
        
    # ### ALTERADO ### Usa a nossa função BR para gravar a saída final
    hora_saida = obter_hora_br().strftime('%Y-%m-%d %H:%M:%S')
    _, valor_total = calcular_tempo_e_valor(ticket['hora_entrada'], hora_saida)

    db.execute(
        "UPDATE TICKETS SET hora_saida = ?, valor_total = ?, status = 'PAGO' WHERE id = ?",
        (hora_saida, valor_total, ticket_id)
    )
    db.commit()
    
    placa_visual = formatar_placa(ticket['placa'])
    entrada_br = formatar_datahora(ticket['hora_entrada'])
    saida_br = formatar_datahora(hora_saida)
    ticket_numero = formatar_ticket_id(ticket_id)
    
    flash(f"Pagamento de R$ {valor_total:.2f} efetuado. Saída liberada.", 'success')
    
    return render_template('recibo_pagamento.html', 
                           ticket_id=ticket_id,
                           valor=valor_total, 
                           placa=placa_visual,
                           entrada=entrada_br, 
                           saida=saida_br,
                           ticket_numero=ticket_numero)


@app.route('/imprimir/<int:ticket_id>')
def imprimir_ticket(ticket_id):
    """Gera uma página simplificada para impressão em formato 'térmico'."""
    db = get_db()
    
    ticket = db.execute(
        "SELECT * FROM TICKETS WHERE id = ? AND status = 'PAGO'", 
        (ticket_id,)
    ).fetchone()

    if not ticket:
        return "Ticket não encontrado ou ainda não pago.", 404

    # Formatação dos dados para o recibo
    placa_visual = formatar_placa(ticket['placa'])
    entrada_br = formatar_datahora(ticket['hora_entrada'])
    saida_br = formatar_datahora(ticket['hora_saida'])
    ticket_numero = formatar_ticket_id(ticket['id'])
    
    return render_template('recibo_impressao.html', 
                           valor=ticket['valor_total'], 
                           placa=placa_visual,
                           entrada=entrada_br, 
                           saida=saida_br,
                           ticket_numero=ticket_numero)

@app.route('/historico')
def historico():
    db = get_db()
    tickets = db.execute(
        "SELECT * FROM TICKETS WHERE status = 'PAGO' ORDER BY hora_saida DESC"
    ).fetchall()
    
    lista_historico = []
    for ticket in tickets:
        lista_historico.append({
            'ticket_numero': formatar_ticket_id(ticket['id']),
            'placa': formatar_placa(ticket['placa']),
            'entrada': formatar_datahora(ticket['hora_entrada']),
            'saida': formatar_datahora(ticket['hora_saida']),
            'valor_total': ticket['valor_total']
        })
        
    return render_template('listar_historico.html', historico=lista_historico)

# --- BLOCO FINAL ---
if __name__ == '__main__':
    init_db()
    app.run(debug=True, port=5000)