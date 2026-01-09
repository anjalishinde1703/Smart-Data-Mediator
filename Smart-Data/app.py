# app.py - Complete with Navigation Routes
import os
import re
import io
import json
import uuid
from datetime import datetime
from functools import wraps

import pandas as pd
from flask import (
    Flask, request, jsonify, render_template, session, redirect, url_for,
    send_file, make_response
)
from flask_session import Session
from werkzeug.utils import secure_filename

# Optional spaCy usage
try:
    import spacy
    nlp = spacy.load("en_core_web_sm")
    SPACY_AVAILABLE = True
except Exception:
    nlp = None
    SPACY_AVAILABLE = False

# PDF generation
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet

# dotenv optional
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# --- App config ---
app = Flask(__name__)
app.config['SESSION_TYPE'] = 'filesystem'
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'default-secret-key')
app.config['UPLOAD_FOLDER'] = os.environ.get('UPLOAD_FOLDER', 'uploads')
app.config['PROJECTS_FOLDER'] = os.environ.get('PROJECTS_FOLDER', 'projects')
app.config['MAX_CONTENT_LENGTH'] = 200 * 1024 * 1024  # 200 MB

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['PROJECTS_FOLDER'], exist_ok=True)

Session(app)

# --- Simple in-memory user store (replace with DB in production) ---
USERS = {
    'admin': {'password': 'admin123', 'role': 'admin'},
    'user': {'password': 'user123', 'role': 'regular'}
}

user_uploads = {}
user_projects = {}
user_reports = {}

ALLOWED_EXT = {'csv', 'xlsx', 'xls'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXT

def login_required(f):
    from functools import wraps
    @wraps(f)
    def wrapped(*args, **kwargs):
        if 'user' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return wrapped

# Synonym map and helpers
SYNONYMS = {
    'region': ['region', 'area', 'zone', 'territory', 'state', 'city'],
    'sales': ['sales', 'revenue', 'turnover', 'income', 'amount'],
    'category': ['category', 'cat', 'type'],
    'product': ['product', 'item', 'sku'],
    'date': ['date', 'order_date', 'sale_date', 'timestamp']
}

def map_token_to_col(token, df_cols):
    token_l = token.lower()
    for col in df_cols:
        col_low = col.lower()
        if token_l == col_low:
            return col
        for key, syns in SYNONYMS.items():
            if token_l in syns and key in col_low:
                return col
        if token_l in col_low or col_low in token_l:
            return col
    return None

def parse_basic_query(query):
    q = query.lower()
    parsed = {'raw': query, 'limit': None, 'filters': {}, 'metric': None, 'intent': 'default', 'group_by': None, 'date_range': None}
    m = re.search(r'top\s+(\d+)', q)
    if m:
        parsed['limit'] = int(m.group(1))
    if 'top' in q and 'product' in q:
        parsed['intent'] = 'top_products'
    elif 'sales by' in q or 'by region' in q or 'sales by region' in q:
        parsed['intent'] = 'sales_by'
    elif 'trend' in q or 'over time' in q or 'trend over' in q:
        parsed['intent'] = 'trend'
    elif 'average' in q or 'mean' in q or 'describe' in q:
        parsed['intent'] = 'stats'
    
    # Fixed regex patterns with proper parentheses
    for match in re.finditer(r'(\w+)\s*=\s*([\w\s\-]+)', q):
        f = match.group(1)
        v = match.group(2).strip()
        parsed['filters'][f] = v
    
    for match in re.finditer(r'in\s+([a-zA-Z0-9\-\_ ]+)', q):
        val = match.group(1).strip()
        parsed['filters'].setdefault('in_values', []).append(val)
    
    # Fixed date pattern with proper parentheses
    date_match = re.search(
        r'(\d{4}(?:-\d{2}(?:-\d{2})?)?)(?:\s*(?:to|-)\s*(\d{4}(?:-\d{2}(?:-\d{2})?)?))?',
        q
    )
    if date_match:
        parsed['date_range'] = (
            date_match.group(1),
            date_match.group(2) if date_match.group(2) else None
        )
    
    for mterm in SYNONYMS['sales']:
        if mterm in q:
            parsed['metric'] = 'sales'
            break
    return parsed

def apply_filters(df, filters, parsed):
    d = df.copy()
    for f, v in filters.items():
        if f == 'in_values':
            for val in v:
                matched = False
                for col in d.columns:
                    try:
                        if d[col].astype(str).str.lower().str.contains(val.lower()).any():
                            d = d[d[col].astype(str).str.lower().str.contains(val.lower())]
                            matched = True
                            break
                    except Exception:
                        continue
        else:
            col = f if f in d.columns else map_token_to_col(f, d.columns)
            if col:
                d = d[d[col].astype(str).str.lower() == str(v).lower()]
    if parsed.get('date_range'):
        dr = parsed['date_range']
        date_col = next((c for c in d.columns if 'date' in c.lower()), None)
        if date_col:
            try:
                d[date_col] = pd.to_datetime(d[date_col], errors='coerce')
                if dr[0] and dr[1]:
                    start = pd.to_datetime(dr[0])
                    end = pd.to_datetime(dr[1])
                    d = d[(d[date_col] >= start) & (d[date_col] <= end)]
                elif dr[0]:
                    if re.fullmatch(r'\d{4}', dr[0]):
                        d = d[d[date_col].dt.year == int(dr[0])]
                    else:
                        d = d[d[date_col] >= pd.to_datetime(dr[0])]
            except Exception:
                pass
    return d

def df_to_records_safe(df):
    df2 = df.copy()
    for c in df2.select_dtypes(include=['datetime64[ns]', 'datetimetz']).columns:
        df2[c] = df2[c].dt.strftime('%Y-%m-%d %H:%M:%S')
    return df2.to_dict('records')

def format_numeric_cols_for_export(df):
    df2 = df.copy()
    num_cols = df2.select_dtypes(include=['number']).columns.tolist()
    for c in num_cols:
        df2[c] = df2[c].round(2).apply(lambda x: f"{x:,.2f}")
    return df2

# --- Core processing ---
def process_natural_language(query, df):
    query_original = query
    parsed = parse_basic_query(query_original)

    metric = parsed.get('metric')
    df_work = df.copy()
    if not metric:
        if any('sales' == c.lower() for c in df_work.columns):
            metric = next(c for c in df_work.columns if c.lower() == 'sales')
        else:
            numeric_cols = df_work.select_dtypes(include='number').columns.tolist()
            metric = numeric_cols[0] if numeric_cols else None
    parsed['metric'] = metric

    mapped_filters = {}
    for fk, fv in parsed['filters'].items():
        if fk == 'in_values':
            mapped_filters['in_values'] = fv
            continue
        mapped_col = fk if fk in df_work.columns else map_token_to_col(fk, df_work.columns)
        if mapped_col:
            mapped_filters[mapped_col] = fv
        else:
            mapped_filters[fk] = fv

    df_filtered = apply_filters(df_work, mapped_filters, parsed)

    if parsed['intent'] == 'top_products':
        product_col = next((c for c in df_filtered.columns if 'product' in c.lower()), None)
        category_col = next((c for c in df_filtered.columns if 'category' in c.lower()), None)
        if not product_col or not parsed['metric']:
            raise ValueError("Dataset missing required columns (product and metric)")
        agg_cols = [product_col]
        if category_col:
            agg_cols.append(category_col)
        res = df_filtered.groupby(agg_cols)[parsed['metric']].sum().reset_index()
        n = parsed.get('limit') or 5
        top_overall = res.sort_values(by=parsed['metric'], ascending=False).head(n).reset_index(drop=True)
        formatted = format_numeric_cols_for_export(top_overall)
        return {'results': df_to_records_safe(formatted), 'type': 'table',
                'chart_data': {'x': product_col, 'y': parsed['metric'], 'group_by': category_col, 'chart_type': 'bar'},
                'meta': parsed}

    elif parsed['intent'] == 'sales_by':
        region_col = next((c for c in df_filtered.columns if 'region' in c.lower() or 'area' in c.lower() or 'city' in c.lower()), None)
        if not region_col:
            cat_cols = [c for c in df_filtered.columns if df_filtered[c].dtype == object]
            region_col = cat_cols[0] if cat_cols else None
            if not region_col:
                raise ValueError("Dataset missing region/categorical column")
        res = df_filtered.groupby(region_col)[parsed['metric']].sum().reset_index()
        formatted = format_numeric_cols_for_export(res)
        return {'results': df_to_records_safe(formatted), 'type': 'table',
                'chart_data': {'x': region_col, 'y': parsed['metric'], 'chart_type': 'bar'}, 'meta': parsed}

    elif parsed['intent'] == 'trend':
        date_col = next((c for c in df_filtered.columns if 'date' in c.lower()), None)
        if not date_col or not parsed['metric']:
            raise ValueError("No date or metric column found for trend analysis")
        df_filtered[date_col] = pd.to_datetime(df_filtered[date_col], errors='coerce')
        trend = df_filtered.set_index(date_col)[parsed['metric']].resample('M').sum().reset_index()
        trend[date_col] = trend[date_col].dt.strftime('%Y-%m')
        formatted = format_numeric_cols_for_export(trend)
        return {'results': df_to_records_safe(formatted), 'type': 'table',
                'chart_data': {'x': date_col, 'y': parsed['metric'], 'chart_type': 'line'}, 'meta': parsed}

    elif parsed['intent'] == 'stats':
        num = df_filtered.select_dtypes(include='number')
        if num.shape[1] == 0:
            raise ValueError("No numeric columns for stats")
        stats = num.describe().round(2).to_dict()
        return {'results': stats, 'type': 'stats', 'meta': parsed}
    else:
        formatted = format_numeric_cols_for_export(df_filtered.head(20))
        return {'results': df_to_records_safe(formatted), 'type': 'table',
                'message': "Showing first rows. Try queries like 'top 5 products' or 'sales by region'", 'meta': parsed}

# --- Routes ---
@app.route('/')
def home():
    if 'user' not in session:
        return redirect(url_for('login'))
    return redirect(url_for('dashboard'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        if username in USERS and USERS[username]['password'] == password:
            session['user'] = {'username': username, 'role': USERS[username]['role']}
            session.permanent = True
            return redirect(url_for('dashboard'))
        return render_template('login.html', error='Invalid credentials')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('user', None)
    session.pop('last_results', None)
    return redirect(url_for('login'))

@app.route('/dashboard')
@login_required
def dashboard():
    usern = session['user']['username']
    recent = user_uploads.get(usern, [])
    projects = user_projects.get(usern, [])
    file_count = len(recent)
    project_count = len(projects)
    weekly_activity = sum(1 for _ in recent)
    last_login = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    return render_template('dashboard.html',
                           user=session['user'],
                           recent_files=recent,
                           file_count=file_count,
                           project_count=project_count,
                           weekly_activity=weekly_activity,
                           last_login=last_login)

@app.route('/upload', methods=['GET', 'POST'])
@login_required
def upload():
    if request.method == 'POST':
        if 'file' not in request.files:
            return jsonify({'error': 'No file part'}), 400
        f = request.files['file']
        if f.filename == '':
            return jsonify({'error': 'No selected file'}), 400
        if f and allowed_file(f.filename):
            filename = secure_filename(f.filename)
            uid = str(uuid.uuid4())
            dest = os.path.join(app.config['UPLOAD_FOLDER'], f"{uid}_{filename}")
            f.save(dest)
            session['current_file'] = {'path': dest, 'filename': filename, 'uploaded': datetime.now().isoformat()}
            entry = {
                'id': uid,
                'filename': filename,
                'path': dest,
                'date_uploaded': datetime.now().isoformat(),
                'size': os.path.getsize(dest)
            }
            user_uploads.setdefault(session['user']['username'], []).insert(0, entry)
            return redirect(url_for('new_project'))
        return jsonify({'error': 'Invalid file type'}), 400
    return render_template('upload.html')

@app.route('/new_project', methods=['GET', 'POST'])
@login_required
def new_project():
    if request.method == 'GET':
        return render_template('new_project.html')
    
    data = request.get_json() or request.form or {}
    query = data.get('query')
    if not query:
        return jsonify({'error': 'Query required'}), 400
    
    current = session.get('current_file')
    if not current:
        return jsonify({'error': 'No data file uploaded'}), 400
    
    path = current['path']
    try:
        df = pd.read_csv(path) if path.lower().endswith('.csv') else pd.read_excel(path)
    except Exception as e:
        return jsonify({'error': f'Failed to read file: {e}'}), 400
    
    try:
        response = process_natural_language(query, df)
        session['last_results'] = {'query': query, 'response': response, 'timestamp': datetime.now().isoformat()}
        
        # Store the report
        report_id = str(uuid.uuid4())
        report_data = {
            'id': report_id,
            'query': query,
            'response': response,
            'timestamp': datetime.now().isoformat(),
            'type': 'basic',
            'file': current['filename']
        }
        user_reports.setdefault(session['user']['username'], []).insert(0, report_data)
        
        return jsonify(response)
    except Exception as e:
        return jsonify({'error': str(e)}), 400

@app.route('/api/process_advanced_query', methods=['POST'])
@login_required
def api_process_advanced_query():
    payload = request.get_json() or {}
    query = payload.get('query')
    if not query:
        return jsonify({'status': 'error', 'error': 'query is required'}), 400
    
    current = session.get('current_file')
    if not current:
        return jsonify({'status': 'error', 'error': 'no data file uploaded'}), 400
    
    path = current['path']
    try:
        df = pd.read_csv(path) if path.lower().endswith('.csv') else pd.read_excel(path)
    except Exception as e:
        return jsonify({'status': 'error', 'error': f'failed to read file: {e}'})
    
    try:
        response = process_natural_language(query, df)
        session['last_results'] = {'query': query, 'response': response, 'timestamp': datetime.now().isoformat()}
        
        # Store the report
        report_id = str(uuid.uuid4())
        report_data = {
            'id': report_id,
            'query': query,
            'response': response,
            'timestamp': datetime.now().isoformat(),
            'type': 'advanced',
            'file': current['filename']
        }
        user_reports.setdefault(session['user']['username'], []).insert(0, report_data)
        
        out = {'status': 'ok'}
        if response.get('type') == 'table':
            out.update({
                'response_type': response.get('chart_data', {}).get('chart_type', 'table') + ('_chart' if response.get('chart_data') else ''),
                'results': response.get('results'),
                'x_axis': response.get('chart_data', {}).get('x'),
                'y_axis': response.get('chart_data', {}).get('y'),
                'group_by': response.get('chart_data', {}).get('group_by'),
                'title': payload.get('title') or query
            })
        elif response.get('type') == 'stats':
            out.update({'response_type': 'stats', 'results': response.get('results')})
        else:
            out.update({'response_type': response.get('type'), 'results': response.get('results')})
        
        return jsonify(out)
    except Exception as e:
        return jsonify({'status': 'error', 'error': str(e)}), 400

@app.route('/api/get_reports')
@login_required
def get_reports():
    reports = user_reports.get(session['user']['username'], [])
    return jsonify({'reports': reports})

@app.route('/process_query', methods=['POST'])
@login_required
def process_query_alias():
    return new_project()

# --- Project saving with versions ---
@app.route('/save_project', methods=['POST'])
@login_required
def save_project():
    data = request.get_json() or {}
    project_name = data.get('name')
    project_data = data.get('data')
    if not project_name:
        return jsonify({'error': 'Project name required'}), 400
    if not project_data:
        return jsonify({'error': 'Project data required'}), 400
    user = session['user']['username']
    project_id = data.get('project_id') or str(uuid.uuid4())
    project_file = os.path.join(app.config['PROJECTS_FOLDER'], f"{project_id}.json")
    versions = []
    if os.path.exists(project_file):
        try:
            with open(project_file, 'r') as f:
                existing = json.load(f)
                versions = existing.get('versions', [])
        except Exception:
            versions = []
    version_number = (versions[-1]['version'] + 1) if versions else 1
    version_entry = {'version': version_number, 'created': datetime.now().isoformat(), 'name': project_name, 'data': project_data}
    versions.append(version_entry)
    project_payload = {'id': project_id, 'owner': user, 'versions': versions}
    try:
        with open(project_file, 'w') as f:
            json.dump(project_payload, f, indent=2)
        user_projects.setdefault(user, [])
        exists = next((p for p in user_projects[user] if p['id'] == project_id), None)
        if not exists:
            user_projects[user].insert(0, {'id': project_id, 'name': project_name, 'created': datetime.now().isoformat(), 'file': f"{project_id}.json"})
        return jsonify({'success': True, 'project_id': project_id, 'version': version_number, 'redirect': f'/project/{project_id}'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/project/<project_id>')
@login_required
def view_project(project_id):
    project_path = os.path.join(app.config['PROJECTS_FOLDER'], f"{project_id}.json")
    if not os.path.exists(project_path):
        return "Project not found", 404
    with open(project_path, 'r') as f:
        project_data = json.load(f)
    latest = project_data.get('versions', [])[-1] if project_data.get('versions') else {}
    return render_template('view_project.html', project_id=project_id, project_data=latest, all_versions=project_data.get('versions', []))

@app.route('/project/<project_id>/versions')
@login_required
def list_project_versions(project_id):
    project_path = os.path.join(app.config['PROJECTS_FOLDER'], f"{project_id}.json")
    if not os.path.exists(project_path):
        return jsonify({'error': 'Project not found'}), 404
    with open(project_path, 'r') as f:
        project_data = json.load(f)
    return jsonify({'versions': project_data.get('versions', [])})

@app.route('/project/<project_id>/restore/<int:version>')
@login_required
def restore_project_version(project_id, version):
    project_path = os.path.join(app.config['PROJECTS_FOLDER'], f"{project_id}.json")
    if not os.path.exists(project_path):
        return jsonify({'error': 'Project not found'}), 404
    with open(project_path, 'r') as f:
        project_data = json.load(f)
    versions = project_data.get('versions', [])
    pick = next((v for v in versions if v['version'] == version), None)
    if not pick:
        return jsonify({'error': 'Version not found'}), 404
    new_version = {'version': versions[-1]['version'] + 1, 'created': datetime.now().isoformat(), 'name': pick.get('name'), 'data': pick.get('data')}
    versions.append(new_version)
    project_data['versions'] = versions
    with open(project_path, 'w') as f:
        json.dump(project_data, f, indent=2)
    return jsonify({'success': True, 'restored_to_version': version, 'new_version': new_version['version']})

# --- Exports (CSV, Excel, PDF, HTML) with styling/formatting ---
@app.route('/export/csv')
@login_required
def export_csv():
    last = session.get('last_results')
    if not last:
        return jsonify({'error': 'No query results to export'}), 400
    resp = last['response']
    rows = resp.get('results')
    if not rows:
        return jsonify({'error': 'No data to export'}), 400
    df = pd.DataFrame(rows)
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    buf.seek(0)
    return send_file(io.BytesIO(buf.getvalue().encode('utf-8')),
                     mimetype='text/csv',
                     as_attachment=True,
                     download_name=f"export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")

@app.route('/export/excel')
@login_required
def export_excel():
    last = session.get('last_results')
    if not last:
        return jsonify({'error': 'No query results to export'}), 400
    resp = last['response']
    rows = resp.get('results')
    if not rows:
        return jsonify({'error': 'No data to export'}), 400
    df = pd.DataFrame(rows)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Export')
    buf.seek(0)
    return send_file(buf,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                     as_attachment=True,
                     download_name=f"export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx")

@app.route('/export/pdf')
@login_required
def export_pdf():
    last = session.get('last_results')
    if not last:
        return jsonify({'error': 'No query results to export'}), 400
    resp = last['response']
    rows = resp.get('results')
    if not rows:
        return jsonify({'error': 'No data to export'}), 400
    df = pd.DataFrame(rows)
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(letter), rightMargin=20, leftMargin=20, topMargin=20, bottomMargin=20)
    styles = getSampleStyleSheet()
    story = []
    title_text = f"Export - {last.get('query', '')}"
    title = Paragraph(title_text, styles['Heading2'])
    story.append(title)
    story.append(Spacer(1, 12))
    data_table = [df.columns.tolist()] + df.fillna('').astype(str).values.tolist()
    tbl = Table(data_table, hAlign='LEFT')
    ncols = len(df.columns)
    total_width = 800
    col_widths = [total_width / ncols] * ncols
    tbl._argW = col_widths
    tbl.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#111827")),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 8),
        ('GRID', (0,0), (-1,-1), 0.25, colors.HexColor("#374151")),
        ('BACKGROUND', (0,1), (-1,-1), colors.HexColor("#0b1220")),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.HexColor("#FFFFFF"), colors.HexColor("#FFFFFF")]),
        ('LEFTPADDING', (0,0), (-1,-1), 6),
        ('RIGHTPADDING', (0,0), (-1,-1), 6),
        ('TOPPADDING', (0,0), (-1,-1), 4),
        ('BOTTOMPADDING', (0,0), (-1,-1), 4),
    ]))
    story.append(tbl)
    doc.build(story)
    buf.seek(0)
    return send_file(buf, mimetype='application/pdf', as_attachment=True,
                     download_name=f"export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf")

@app.route('/export/html')
@login_required
def export_html():
    last = session.get('last_results')
    if not last:
        return jsonify({'error': 'No query results to export'}), 400
    resp = last['response']
    rows = resp.get('results')
    if not rows:
        return jsonify({'error': 'No data to export'}), 400
    df = pd.DataFrame(rows)
    css = """
    <style>
    body{background:#0f172a;color:#e5e7eb;font-family:Inter,Arial,Helvetica,sans-serif;padding:24px;}
    .export-table{border-collapse:collapse;width:100%;max-width:900px;}
    .export-table th{background:#111827;color:#fff;padding:10px;border-bottom:1px solid #374151;text-align:left;}
    .export-table td{background:#0b1220;color:#e5e7eb;padding:10px;border-bottom:1px solid #111827;}
    .container{background:linear-gradient(180deg, rgba(255,255,255,0.02), rgba(255,255,255,0.01));padding:20px;border-radius:12px;}
    h2{color:#fff;}
    </style>
    """
    html_table = '<table class="export-table"><thead><tr>'
    cols = df.columns.tolist()
    for c in cols:
        html_table += f'<th>{c}</th>'
    html_table += '</tr></thead><tbody>'
    for _, row in df.iterrows():
        html_table += '<tr>'
        for c in cols:
            html_table += f'<td>{row[c]}</td>'
        html_table += '</tr>'
    html_table += '</tbody></table>'
    html = f"""<!doctype html><html><head><meta charset="utf-8"><title>Export</title>{css}</head>
    <body><div class="container"><h2>Export - {last.get('query','')}</h2>{html_table}</div></body></html>"""
    response = make_response(html)
    response.headers['Content-Disposition'] = f'attachment; filename=export_{datetime.now().strftime("%Y%m%d_%H%M%S")}.html'
    response.headers['Content-Type'] = 'text/html'
    return response

# --- Navigation Routes ---
@app.route('/templates')
@login_required
def templates():
    usern = session['user']['username']
    recent = user_uploads.get(usern, [])
    return render_template('templates.html',
                         user=session['user'],
                         recent_files=recent,
                         last_login=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))

@app.route('/projects')
@login_required
def projects():
    usern = session['user']['username']
    projects = user_projects.get(usern, [])
    return render_template('projects.html',
                         user=session['user'],
                         projects=projects,
                         last_login=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))

@app.route('/reports')
@login_required
def reports():
    usern = session['user']['username']
    recent = user_uploads.get(usern, [])
    projects = user_projects.get(usern, [])
    return render_template('reports.html',
                         user=session['user'],
                         recent_files=recent,
                         projects=projects,
                         last_login=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))

@app.route('/data')
@login_required
def data_sources():
    usern = session['user']['username']
    recent = user_uploads.get(usern, [])
    return render_template('data_sources.html',
                         user=session['user'],
                         recent_files=recent,
                         last_login=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))

@app.route('/settings')
@login_required
def settings():
    return render_template('settings.html',
                         user=session['user'],
                         last_login=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))

@app.route('/profile')
@login_required
def profile():
    return render_template('profile.html',
                         user=session['user'],
                         last_login=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))

@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'spacy': SPACY_AVAILABLE})

if __name__ == '__main__':
    app.run(debug=True, port=int(os.environ.get('PORT', 5000)))