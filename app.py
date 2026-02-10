import pandas as pd
import numpy as np
import os
import sys
import xmlrpc.client
import webbrowser
import json
import traceback
from threading import Timer
from flask import Flask, render_template, request, jsonify, session, redirect, url_for

# Try importing waitress
try:
    from waitress import serve
except ImportError:
    print("ERROR: 'waitress' library is missing. Please run: pip install waitress")
    input("Press Enter to exit...")
    sys.exit(1)

# --- 1. CONFIGURATION ---
try:
    if getattr(sys, 'frozen', False):
        BASE_DIR = sys._MEIPASS
        EXE_DIR = os.path.dirname(sys.executable)
    else:
        BASE_DIR = os.path.dirname(os.path.abspath(__file__))
        EXE_DIR = BASE_DIR

    app = Flask(__name__, 
                template_folder=os.path.join(BASE_DIR, 'templates'), 
                static_folder=os.path.join(BASE_DIR, 'static'))

    app.secret_key = 'super_secret_sales_app_key_change_this' 

    # Ensure 'labels' folder exists
    LABELS_DIR = os.path.join(EXE_DIR, 'labels')
    if not os.path.exists(LABELS_DIR):
        os.makedirs(LABELS_DIR)

    print(f"=== SALES APP STARTED (VERSION 77.0 - USER PERMISSIONS FIX) ===")
    print(f"Running from: {EXE_DIR}")
    print(f"Labels Directory: {LABELS_DIR}")

except Exception as e:
    print("CRITICAL ERROR DURING CONFIGURATION:")
    traceback.print_exc()
    input("Press Enter to exit...")
    sys.exit(1)

# ==========================================
#     USER CONFIGURATION
# ==========================================
ODOO_URL = "https://ug-group-erp.odoo.com/"
ODOO_DB = "alliontechnologies-odoo-uni-gro-master-1235186"
ODOO_USER = "hariramanumakanth@gmail.com"
ODOO_PASS = "71@Galleroad"
ODOO_COMPANY = "uni gro"
# ==========================================

def get_users():
    try:
        json_path = os.path.join(EXE_DIR, 'users.json')
        if not os.path.exists(json_path): return {"admin": "admin"}
        with open(json_path, 'r') as f: return json.load(f)
    except: return {"admin": "admin"}

@app.before_request
def require_login():
    if request.endpoint not in ['login', 'static'] and 'user' not in session:
        return redirect(url_for('login'))
    if 'user' in session:
        user = session.get('user')
        # FIXED: Removed 'generate_report_odoo' and 'api_generate_summary_from_ids' so users can run reports
        restricted = ['purchase', 'fetch_purchase_orders', 'reports', 'product_performance_page', 'api_product_performance_report', 'product_sales_page', 'api_product_sales_report', 'product_search_page', 'product_history', 'api_search_product', 'api_product_moves', 'sales_team_report_page', 'api_sales_team_report', 'scraper', 'parse_text', 'order_form_page', 'api_create_sales_order', 'fetch_odoo_products', 'print_labels_file']
        
        if user != 'admin' and request.endpoint in restricted:
            return redirect(url_for('index'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        users = get_users()
        if request.form.get('username') in users and users[request.form.get('username')] == request.form.get('password'):
            session['user'] = request.form.get('username')
            return redirect(url_for('index'))
        else: return render_template('login.html', error="Invalid Credentials")
    return render_template('login.html')

@app.route('/logout')
def logout(): session.pop('user', None); return redirect(url_for('login'))

# ==========================================
#           DATA LOADERS (HELPER FUNCTIONS)
# ==========================================

def get_odoo_connection():
    url = ODOO_URL.rstrip('/')
    common = xmlrpc.client.ServerProxy(f'{url}/xmlrpc/2/common', allow_none=True)
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASS, {})
    models = xmlrpc.client.ServerProxy(f'{url}/xmlrpc/2/object', allow_none=True)
    return uid, models

def get_company_id(uid, models):
    ids = models.execute_kw(ODOO_DB, uid, ODOO_PASS, 'res.company', 'search', [[['name', 'ilike', ODOO_COMPANY]]])
    return ids[0] if ids else None

def get_context(company_id):
    ctx = {'tz': 'Asia/Colombo'}
    if company_id: ctx['allowed_company_ids'] = [company_id]
    return ctx

def fetch_odoo_dataframe(uid, models, model, domain, field_map):
    if model == 'account.move.line':
        domain.append(('exclude_from_invoice_tab', '=', False))
    
    cid = get_company_id(uid, models)
    ctx = get_context(cid)
    
    records = models.execute_kw(ODOO_DB, uid, ODOO_PASS, model, 'search_read', [domain], {'fields': list(field_map.keys()), 'limit': 50000, 'context': ctx})
    data = []
    for r in records:
        row = {}
        for k, v in field_map.items():
            val = r.get(k, '')
            row[v] = val[1] if isinstance(val, (list, tuple)) else (val if val is not False else "")
        data.append(row)
    
    # Return empty DataFrame with correct columns if no data
    if not data:
        return pd.DataFrame(columns=list(field_map.values()))
        
    return pd.DataFrame(data)

def calculate_discounts(uid, models, ids):
    if not ids: return {}
    disc = {}
    chunk = 500
    for i in range(0, len(ids), chunk):
        lines = models.execute_kw(ODOO_DB, uid, ODOO_PASS, 'account.move.line', 'search_read', [[('move_id', 'in', ids[i:i+chunk]), ('exclude_from_invoice_tab', '=', False)]], {'fields': ['move_id', 'quantity', 'price_unit', 'price_subtotal']})
        for l in lines:
            d = (l.get('quantity', 0.0) * l.get('price_unit', 0.0)) - l.get('price_subtotal', 0.0)
            disc[l['move_id'][0]] = disc.get(l['move_id'][0], 0.0) + max(0.0, d)
    return disc

# ==========================================
#      SUMMARY LOGIC
# ==========================================
def analyze_frames(df_sales, df_pay, df_ret, start_date, end_date):
    try:
        df_sales['Date'] = pd.to_datetime(df_sales['Date'])
        df_ret['Date'] = pd.to_datetime(df_ret['Date'])
        
        if not start_date and not df_sales.empty: start_date = df_sales['Date'].min()
        if not end_date and not df_sales.empty: end_date = df_sales['Date'].max()
        
        if not start_date or not end_date:
             s_date = pd.Timestamp.now()
             e_date = pd.Timestamp.now()
        else:
             s_date, e_date = pd.to_datetime(start_date), pd.to_datetime(end_date)

        fs = df_sales[(df_sales['Date'] >= s_date) & (df_sales['Date'] <= e_date)].copy()
        fr = df_ret[(df_ret['Date'] >= s_date) & (df_ret['Date'] <= e_date)].copy()
    except Exception as e: 
        print(f"Analysis Error: {e}")
        raise ValueError(f"Date Error: {str(e)}")

    for df in [fs, fr]:
        df['Total'] = pd.to_numeric(df['Total'], errors='coerce').fillna(0)
        df['Discount'] = pd.to_numeric(df['Discount'], errors='coerce').fillna(0)
        if 'Residual' in df.columns: df['Residual'] = pd.to_numeric(df['Residual'], errors='coerce')
        else: df['Residual'] = np.nan

    fs['Sales Value'] = fs['Total']; fs['Return Value'] = 0.0; fs['Signed Amount'] = fs['Total']
    fr['Sales Value'] = 0.0; fr['Return Value'] = fr['Total']; fr['Signed Amount'] = fr['Total'] * -1
    fr['Residual'] = fr['Residual'] * -1

    cols = ['Date', 'Number', 'Partner/Name', 'Discount', 'Sales Team/Sales Team', 'Sales Value', 'Return Value', 'Signed Amount', 'Residual']
    
    # Ensure columns exist
    for c in cols:
        if c not in fs.columns: fs[c] = 0 if c in ['Sales Value','Return Value','Signed Amount','Discount'] else ''
        if c not in fr.columns: fr[c] = 0 if c in ['Sales Value','Return Value','Signed Amount','Discount'] else ''

    combined = pd.concat([fs[cols], fr[cols]], ignore_index=True).sort_values('Date')

    if 'Payment Type Name' not in df_pay.columns: df_pay['Payment Type Name'] = 'Unspecified'
    
    if not df_pay.empty:
        pay_grp = df_pay.groupby(['Reference', 'Payment Type Name'])['Amount'].sum().unstack(fill_value=0).reset_index()
        pay_grp.columns = [str(c) for c in pay_grp.columns]
        merged = pd.merge(combined, pay_grp, left_on='Number', right_on='Reference', how='left')
        p_cols = [c for c in pay_grp.columns if c != 'Reference']
    else:
        merged = combined.copy()
        merged['Total Paid (Cash)'] = 0
        p_cols = []

    if p_cols:
        merged[p_cols] = merged[p_cols].fillna(0)
        merged['Total Paid (Cash)'] = merged[p_cols].sum(axis=1)
    else:
        merged['Total Paid (Cash)'] = 0

    def get_true_due(row):
        if not pd.isna(row['Residual']): return row['Residual']
        return row['Signed Amount'] - row['Total Paid (Cash)']

    merged['Due'] = merged.apply(get_true_due, axis=1)
    merged['Total Paid'] = merged['Signed Amount'] - merged['Due']
    merged.rename(columns={'Number': 'Invoice No', 'Partner/Name': 'Customer', 'Sales Team/Sales Team': 'Department'}, inplace=True)
    merged.loc[merged['Invoice No'].astype(str).str.upper().str.startswith('RSAL'), 'Due'] = 0

    final_cols = ['Invoice No', 'Customer', 'Sales Value', 'Due', 'Return Value', 'Discount', 'Department']
    for c in final_cols: 
        if c not in merged.columns: merged[c] = 0

    summary = {
        'gross_sales': float(fs['Total'].sum()),
        'total_discount': float(combined['Discount'].sum()),
        'total_returns': float(fr['Total'].sum()),
        'net_sales': float(fs['Total'].sum() - fr['Total'].sum())
    }
    
    if not merged.empty:
        dept = merged.groupby('Department')[['Signed Amount', 'Discount', 'Return Value', 'Due']].sum()
        dept_sales = dept['Signed Amount'].to_dict()
        dept_disc = dept['Discount'].to_dict()
        dept_ret = dept['Return Value'].to_dict()
        dept_due = dept['Due'].to_dict()
    else:
        dept_sales = {}; dept_disc = {}; dept_ret = {}; dept_due = {}

    data = merged.fillna(0).to_dict(orient='records')
    for r in data:
        for k,v in r.items(): 
            if isinstance(v, (np.floating, float)): r[k] = float(v)

    return {'data': data, 'columns': final_cols, 'summary': summary, 'payment_summary': {c: float(merged[c].sum()) for c in p_cols}, 'total_due': float(merged['Due'].sum()), 'dept_sales': dept_sales, 'dept_discount': dept_disc, 'dept_returns': dept_ret, 'dept_due': dept_due}

def format_res(res):
    totals = {}
    for c in res['columns']:
        if c in ['Sales Value', 'Due', 'Return Value', 'Discount']:
            totals[c] = float(sum(r[c] for r in res['data'] if isinstance(r[c], (int, float))))
        else: totals[c] = ""
    totals['Customer'] = "TOTALS"
    res['totals'] = totals
    return jsonify(res)

# ==========================================
#           ROUTES & APIs
# ==========================================
@app.route('/')
def index(): return render_template('index.html', is_admin=(session.get('user')=='admin'))
@app.route('/summary')
def summary(): return render_template('summary.html')
@app.route('/labels')
def labels(): return render_template('labels.html')
@app.route('/purchase')
def purchase(): return render_template('purchase.html')
@app.route('/reports')
def reports(): return render_template('reports.html')
@app.route('/product_search')
def product_search_page(): return render_template('product_search.html')
@app.route('/product_sales')
def product_sales_page(): return render_template('product_sales.html')
@app.route('/product_performance')
def product_performance_page(): return render_template('product_performance.html')
@app.route('/sales_team_report')
def sales_team_report_page(): return render_template('sales_team_report.html')
@app.route('/order_form')
def order_form_page(): return render_template('order_form.html')
@app.route('/advanced_summary')
def advanced_summary_page(): return render_template('advanced_summary.html')
@app.route('/scraper')
def scraper(): return render_template('scraper.html')
@app.route('/product_history/<int:product_id>')
def product_history_page(product_id): return render_template('product_history.html')

# --- GENERATE SUMMARY ---
@app.route('/generate_from_odoo', methods=['POST'])
def generate_report_odoo():
    try:
        s, e = request.json.get('startDate'), request.json.get('endDate')
        uid, m = get_odoo_connection()
        cid = get_company_id(uid, m)
        dom = [('invoice_date', '>=', s), ('invoice_date', '<=', e)]
        if cid: dom.append(('company_id', '=', cid))
        
        sales_map = {'id':'id', 'name':'Number', 'invoice_date':'Date', 'partner_id':'Partner/Name', 'amount_total':'Total', 'amount_residual':'Residual', 'team_id':'Sales Team/Sales Team'}
        df_sales = fetch_odoo_dataframe(uid, m, 'account.move', dom + [('move_type','=','out_invoice'), ('state','=','posted')], sales_map)
        df_ret = fetch_odoo_dataframe(uid, m, 'account.move', dom + [('move_type','=','out_refund'), ('state','=','posted')], sales_map)
        
        if not df_sales.empty: df_sales['Discount'] = df_sales['id'].map(calculate_discounts(uid, m, df_sales['id'].tolist())).fillna(0)
        else: df_sales['Discount'] = 0.0
        
        if not df_ret.empty: df_ret['Discount'] = df_ret['id'].map(calculate_discounts(uid, m, df_ret['id'].tolist())).fillna(0)
        else: df_ret['Discount'] = 0.0
        
        p_map = {'amount':'Amount', 'date':'Date', 'ref':'Reference', 'journal_id':'Payment Type Name'}
        df_pay = fetch_odoo_dataframe(uid, m, 'account.payment', [('date', '>=', s), ('date', '<=', e), ('payment_type','=','inbound'), ('state','=','posted')] + ([('company_id','=',cid)] if cid else []), p_map)
        
        return format_res(analyze_frames(df_sales, df_pay, df_ret, s, e))
    except Exception as x:
        print(f"Generate Report Error: {x}") 
        return jsonify({'error': str(x)}), 500

# --- ADVANCED SUMMARY ---
@app.route('/api/generate_summary_from_ids', methods=['POST'])
def api_generate_summary_from_ids():
    try:
        ids = request.json.get('move_ids', [])
        uid, m = get_odoo_connection()
        fields = ['id', 'name', 'invoice_date', 'partner_id', 'amount_total', 'amount_residual', 'team_id', 'move_type']
        recs = m.execute_kw(ODOO_DB, uid, ODOO_PASS, 'account.move', 'read', [ids], {'fields': fields})
        s_data, r_data, refs = [], [], []
        
        for r in recs:
            row = {
                'id': r['id'], 
                'Date': r['invoice_date'], 
                'Number': r['name'], 
                'Partner/Name': r['partner_id'][1] if r['partner_id'] else '', 
                'Total': r['amount_total'], 
                'Residual': r.get('amount_residual', 0.0), 
                'Sales Team/Sales Team': r['team_id'][1] if r['team_id'] else ''
            }
            refs.append(r['name'])
            if r['move_type'] == 'out_invoice': s_data.append(row)
            else: r_data.append(row)
            
        required_cols = ['Date', 'Number', 'Partner/Name', 'Sales Team/Sales Team', 'Total', 'Residual', 'Discount']
        dfs = pd.DataFrame(s_data) if s_data else pd.DataFrame(columns=required_cols)
        dfr = pd.DataFrame(r_data) if r_data else pd.DataFrame(columns=required_cols)
        
        if not dfs.empty: dfs['Discount'] = dfs['id'].map(calculate_discounts(uid, m, dfs['id'].tolist())).fillna(0)
        else: dfs['Discount'] = 0
        if not dfr.empty: dfr['Discount'] = dfr['id'].map(calculate_discounts(uid, m, dfr['id'].tolist())).fillna(0)
        else: dfr['Discount'] = 0
        
        dfp = pd.DataFrame()
        if refs:
            pay_dom = [('ref', 'in', refs), ('state', '=', 'posted')]
            cid = get_company_id(uid, m)
            if cid: pay_dom.append(('company_id', '=', cid))
            pays = m.execute_kw(ODOO_DB, uid, ODOO_PASS, 'account.payment', 'search_read', [pay_dom], {'fields': ['amount', 'date', 'ref', 'journal_id'], 'limit': 5000})
            p_data = [{'Amount': p['amount'], 'Date': p['date'], 'Reference': p['ref'], 'Payment Type Name': p['journal_id'][1] if p['journal_id'] else ''} for p in pays]
            dfp = pd.DataFrame(p_data)
            
        if dfp.empty: dfp = pd.DataFrame(columns=['Amount', 'Date', 'Reference', 'Payment Type Name'])
        
        analysis = analyze_frames(dfs, dfp, dfr, None, None)
        totals = {}
        for c in analysis['columns']:
            if c in ['Sales Value', 'Due', 'Return Value', 'Discount']:
                totals[c] = float(sum(r[c] for r in analysis['data'] if isinstance(r[c], (int, float))))
            else: totals[c] = ""
        totals['Customer'] = "TOTALS"
        analysis['totals'] = totals
        
        return jsonify({'success': True, 'result': analysis})
    except Exception as x: return jsonify({'success': False, 'error': str(x)}), 500

# --- PRODUCT MOVES (HISTORY) ---
@app.route('/api/product_moves', methods=['POST'])
def api_product_moves():
    try:
        data = request.json
        pid = int(data.get('product_id'))
        start = data.get('start')
        end = data.get('end')
        
        uid, m = get_odoo_connection()
        cid = get_company_id(uid, m)
        ctx = get_context(cid)
        ctx['active_test'] = False # Allow archived
        
        domain = [('product_id', '=', pid), ('state', '=', 'done'), ('date', '>=', start), ('date', '<=', end)]
        if cid: domain.append(('company_id', '=', cid))
        
        moves = m.execute_kw(ODOO_DB, uid, ODOO_PASS, 'stock.move.line', 'search_read', 
            [domain], 
            {'fields': ['date', 'reference', 'qty_done', 'location_id', 'location_dest_id'], 'order': 'date desc', 'limit': 500, 'context': ctx})
            
        result = []
        for mv in moves:
            loc_src = mv['location_id'][1] if mv['location_id'] else ''
            loc_dst = mv['location_dest_id'][1] if mv['location_dest_id'] else ''
            result.append({
                'date': mv['date'],
                'reference': mv.get('reference', ''),
                'qty': mv.get('qty_done', 0),
                'location_src': loc_src,
                'location_dest': loc_dst
            })
            
        stock = {'on_hand': 0, 'forecast': 0}
        try:
            prod_data = m.execute_kw(ODOO_DB, uid, ODOO_PASS, 'product.product', 'read', [[pid]], {'fields': ['qty_available', 'virtual_available'], 'context': ctx})
            if prod_data:
                stock['on_hand'] = prod_data[0].get('qty_available', 0)
                stock['forecast'] = prod_data[0].get('virtual_available', 0)
        except: pass
        
        return jsonify({'success': True, 'moves': result, 'stock': stock})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# --- OTHER APIs ---
@app.route('/api/search_product', methods=['POST'])
def api_search_product():
    try:
        term = request.json.get('term', '').strip()
        uid, m = get_odoo_connection(); cid = get_company_id(uid, m); ctx = get_context(cid)
        dom = ['|', '|', ('name', 'ilike', term), ('default_code', 'ilike', term), ('barcode', 'ilike', term)]
        dom = [('active', '=', True)] + dom
        prods = m.execute_kw(ODOO_DB, uid, ODOO_PASS, 'product.product', 'search_read', [dom], {'fields': ['id', 'name', 'default_code', 'barcode', 'list_price'], 'limit': 20, 'context': ctx})
        return jsonify({'success': True, 'products': prods})
    except Exception as e: return jsonify({'error': str(e)}), 500

@app.route('/fetch_purchase_orders', methods=['POST'])
def fetch_purchase_orders():
    try:
        data = request.json
        start = data.get('startDate'); end = data.get('endDate'); vendor = data.get('vendor', '').strip()
        uid, models = get_odoo_connection(); cid = get_company_id(uid, models); ctx = get_context(cid)
        domain = [('date_order', '>=', start), ('date_order', '<=', end)]
        if cid: domain.append(('company_id', '=', cid))
        if vendor: domain.append(('partner_id', 'ilike', vendor))
        orders = models.execute_kw(ODOO_DB, uid, ODOO_PASS, 'purchase.order', 'search_read', [domain], {'fields': ['id', 'name', 'date_order', 'partner_id', 'amount_total', 'state'], 'limit': 2000, 'context': ctx})
        order_ids = [o['id'] for o in orders]
        lines_map = {}
        if order_ids:
            lines = models.execute_kw(ODOO_DB, uid, ODOO_PASS, 'purchase.order.line', 'search_read', [[('order_id', 'in', order_ids)]], {'fields': ['order_id', 'product_id', 'product_qty', 'price_unit', 'price_subtotal'], 'context': ctx})
            for l in lines:
                oid = l['order_id'][0]
                if oid not in lines_map: lines_map[oid] = []
                p_name = l['product_id'][1] if l['product_id'] else 'Unknown'
                lines_map[oid].append({'product': p_name, 'qty': l.get('product_qty', 0), 'price_unit': l.get('price_unit', 0.0), 'price_subtotal': l.get('price_subtotal', 0.0)})
        result = []
        for o in orders:
            p_name = o['partner_id'][1] if o['partner_id'] else 'Unknown'
            result.append({'id': o['id'], 'name': o['name'], 'date_order': o['date_order'].split(' ')[0] if o['date_order'] else '', 'partner_id': p_name, 'amount_total': o.get('amount_total', 0.0), 'state': o.get('state', ''), 'lines': lines_map.get(o['id'], [])})
        return jsonify({'success': True, 'orders': result})
    except Exception as e: return jsonify({'error': str(e)}), 500

@app.route('/api/product_sales_report', methods=['POST'])
def api_product_sales_report():
    try:
        data = request.json; start = data.get('start'); end = data.get('end')
        uid, models = get_odoo_connection(); cid = get_company_id(uid, models); ctx = get_context(cid)
        dom_sales = [('move_id.invoice_date', '>=', start), ('move_id.invoice_date', '<=', end), ('move_id.state', '=', 'posted'), ('move_id.move_type', '=', 'out_invoice'), ('exclude_from_invoice_tab', '=', False)]
        if cid: dom_sales.append(('company_id', '=', cid))
        sales_groups = models.execute_kw(ODOO_DB, uid, ODOO_PASS, 'account.move.line', 'read_group', [dom_sales, ['product_id', 'quantity', 'price_subtotal'], ['product_id']], {'context': ctx})
        dom_ret = [('move_id.invoice_date', '>=', start), ('move_id.invoice_date', '<=', end), ('move_id.state', '=', 'posted'), ('move_id.move_type', '=', 'out_refund'), ('exclude_from_invoice_tab', '=', False)]
        if cid: dom_ret.append(('company_id', '=', cid))
        ret_groups = models.execute_kw(ODOO_DB, uid, ODOO_PASS, 'account.move.line', 'read_group', [dom_ret, ['product_id', 'quantity', 'price_subtotal'], ['product_id']], {'context': ctx})
        stats = {}; all_pids = set()
        for g in sales_groups:
            if not g['product_id']: continue
            pid = g['product_id'][0]; all_pids.add(pid)
            if pid not in stats: stats[pid] = {'qty': 0.0, 'val': 0.0}
            stats[pid]['qty'] += g.get('quantity', 0.0); stats[pid]['val'] += g.get('price_subtotal', 0.0)
        for g in ret_groups:
            if not g['product_id']: continue
            pid = g['product_id'][0]; all_pids.add(pid)
            if pid not in stats: stats[pid] = {'qty': 0.0, 'val': 0.0}
            stats[pid]['qty'] -= abs(g.get('quantity', 0.0)); stats[pid]['val'] -= abs(g.get('price_subtotal', 0.0))
        if not all_pids: return jsonify({'success': True, 'report': []})
        p_list = list(all_pids); products = []; chunk_size = 5000; ctx['active_test'] = False 
        for i in range(0, len(p_list), chunk_size):
            chunk = p_list[i:i + chunk_size]
            products.extend(models.execute_kw(ODOO_DB, uid, ODOO_PASS, 'product.product', 'read', [chunk], {'fields': ['name', 'default_code', 'barcode', 'standard_price', 'list_price', 'seller_ids'], 'context': ctx}))
        seller_ids = list(set([p['seller_ids'][0] for p in products if p['seller_ids']])); seller_map = {}
        if seller_ids:
            for i in range(0, len(seller_ids), chunk_size):
                chunk = seller_ids[i:i + chunk_size]
                sinfos = models.execute_kw(ODOO_DB, uid, ODOO_PASS, 'product.supplierinfo', 'read', [chunk], {'fields': ['name'], 'context': ctx})
                for si in sinfos: 
                    if si['name']: seller_map[si['id']] = si['name'][1]
        report_data = []
        for p in products:
            pid = p['id']; st = stats.get(pid, {'qty': 0, 'val': 0})
            if st['qty'] == 0 and st['val'] == 0: continue
            v_name = seller_map.get(p['seller_ids'][0], "-") if p['seller_ids'] else "-"
            report_data.append({'barcode': p.get('barcode') or '-', 'ref': p.get('default_code') or '-', 'name': p.get('name', 'Unknown'), 'vendor': v_name, 'cost': p.get('standard_price', 0.0), 'price': p.get('list_price', 0.0), 'qty': st['qty'], 'sales_value': st['val']})
        report_data.sort(key=lambda x: x['sales_value'], reverse=True)
        return jsonify({'success': True, 'report': report_data})
    except Exception as e: return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/product_performance_report', methods=['POST'])
def api_product_performance_report():
    try:
        data = request.json; start = data.get('start'); end = data.get('end')
        uid, models = get_odoo_connection(); cid = get_company_id(uid, models); ctx = get_context(cid)
        dom_sales = [('move_id.invoice_date', '>=', start), ('move_id.invoice_date', '<=', end), ('move_id.state', '=', 'posted'), ('move_id.move_type', '=', 'out_invoice'), ('exclude_from_invoice_tab', '=', False)]
        if cid: dom_sales.append(('company_id', '=', cid))
        sales_groups = models.execute_kw(ODOO_DB, uid, ODOO_PASS, 'account.move.line', 'read_group', [dom_sales, ['product_id', 'quantity', 'price_subtotal'], ['product_id']], {'context': ctx})
        dom_ret = [('move_id.invoice_date', '>=', start), ('move_id.invoice_date', '<=', end), ('move_id.state', '=', 'posted'), ('move_id.move_type', '=', 'out_refund'), ('exclude_from_invoice_tab', '=', False)]
        if cid: dom_ret.append(('company_id', '=', cid))
        ret_groups = models.execute_kw(ODOO_DB, uid, ODOO_PASS, 'account.move.line', 'read_group', [dom_ret, ['product_id', 'quantity', 'price_subtotal'], ['product_id']], {'context': ctx})
        dom_pur = [('order_id.date_order', '>=', start), ('order_id.date_order', '<=', end), ('state', 'in', ['purchase', 'done'])]
        if cid: dom_pur.append(('company_id', '=', cid))
        pur_groups = models.execute_kw(ODOO_DB, uid, ODOO_PASS, 'purchase.order.line', 'read_group', [dom_pur, ['product_id', 'product_qty', 'price_subtotal'], ['product_id']], {'context': ctx})
        dom_vr = [('move_id.invoice_date', '>=', start), ('move_id.invoice_date', '<=', end), ('move_id.state', '=', 'posted'), ('move_id.move_type', '=', 'in_refund'), ('exclude_from_invoice_tab', '=', False)]
        if cid: dom_vr.append(('company_id', '=', cid))
        vr_groups = models.execute_kw(ODOO_DB, uid, ODOO_PASS, 'account.move.line', 'read_group', [dom_vr, ['product_id', 'quantity'], ['product_id']], {'context': ctx})
        all_product_ids = set(); product_stats = {} 
        def get_stats(pid):
            if pid not in product_stats: product_stats[pid] = {'s_qty': 0.0, 's_val': 0.0, 'cr_qty': 0.0, 'cr_val': 0.0, 'vr_qty': 0.0, 'p_qty': 0.0}
            return product_stats[pid]
        for g in sales_groups:
            if g['product_id']: pid = g['product_id'][0]; all_product_ids.add(pid); st = get_stats(pid); st['s_qty'] += g.get('quantity', 0.0); st['s_val'] += g.get('price_subtotal', 0.0)
        for g in ret_groups:
            if g['product_id']: pid = g['product_id'][0]; all_product_ids.add(pid); st = get_stats(pid); st['cr_qty'] += abs(g.get('quantity', 0.0)); st['cr_val'] += abs(g.get('price_subtotal', 0.0))
        for g in pur_groups:
            if g['product_id']: pid = g['product_id'][0]; all_product_ids.add(pid); st = get_stats(pid); st['p_qty'] += g.get('product_qty', 0.0)
        for g in vr_groups:
            if g['product_id']: pid = g['product_id'][0]; all_product_ids.add(pid); st = get_stats(pid); st['vr_qty'] += abs(g.get('quantity', 0.0))
        active_ids = models.execute_kw(ODOO_DB, uid, ODOO_PASS, 'product.product', 'search', [[('active', '=', True)]], {'context': ctx})
        all_product_ids.update(active_ids)
        if not all_product_ids: return jsonify({'success': True, 'report': []})
        product_list = list(all_product_ids); products = []; chunk_size = 5000; ctx['active_test'] = False 
        for i in range(0, len(product_list), chunk_size):
            chunk = product_list[i:i + chunk_size]
            products.extend(models.execute_kw(ODOO_DB, uid, ODOO_PASS, 'product.product', 'read', [chunk], {'fields': ['name', 'default_code', 'barcode', 'standard_price', 'list_price', 'qty_available', 'seller_ids'], 'context': ctx}))
        seller_ids = list(set([p['seller_ids'][0] for p in products if p['seller_ids']])); seller_map = {}
        if seller_ids:
            for i in range(0, len(seller_ids), chunk_size):
                chunk = seller_ids[i:i + chunk_size]
                sinfos = models.execute_kw(ODOO_DB, uid, ODOO_PASS, 'product.supplierinfo', 'read', [chunk], {'fields': ['name'], 'context': ctx})
                for si in sinfos:
                    if si['name']: seller_map[si['id']] = si['name'][1]
        report_data = []
        for p in products:
            pid = p['id']; stats = product_stats.get(pid, {'s_qty': 0.0, 's_val': 0.0, 'cr_qty': 0.0, 'cr_val': 0.0, 'vr_qty': 0.0, 'p_qty': 0.0})
            v_name = seller_map.get(p['seller_ids'][0], "-") if p['seller_ids'] else "-"
            report_data.append({'barcode': p.get('barcode') or '-', 'ref': p.get('default_code') or '-', 'name': p.get('name', 'Unknown'), 'vendor': v_name, 'cost': p.get('standard_price', 0.0), 'price': p.get('list_price', 0.0), 'pur_qty': stats['p_qty'], 'ret_qty': stats['vr_qty'], 'net_pur': stats['p_qty'] - stats['vr_qty'], 'sold_qty': stats['s_qty'] - stats['cr_qty'], 'sales_val': stats['s_val'] - stats['cr_val'], 'on_hand': p.get('qty_available', 0.0)})
        return jsonify({'success': True, 'report': report_data})
    except Exception as e: return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/fetch_invoices_for_selection', methods=['POST'])
def api_fetch_invoices_for_selection():
    try:
        data = request.json
        uid, m = get_odoo_connection()
        cid = get_company_id(uid, m)
        dom = [('move_type', 'in', ['out_invoice', 'out_refund']), ('state', '=', 'posted'), ('invoice_date', '>=', data['start']), ('invoice_date', '<=', data['end'])]
        if cid: dom.append(('company_id', '=', cid))
        recs = m.execute_kw(ODOO_DB, uid, ODOO_PASS, 'account.move', 'search_read', [dom], {'fields': ['id', 'name', 'invoice_date', 'partner_id', 'amount_total', 'move_type']})
        clean = [{'id': r['id'], 'date': r['invoice_date'], 'number': r['name'], 'partner': r['partner_id'][1] if r['partner_id'] else '', 'amount': r['amount_total'], 'type': r['move_type']} for r in recs]
        return jsonify({'success': True, 'invoices': clean})
    except Exception as e: return jsonify({'error': str(e)}), 500

@app.route('/api/get_sales_teams', methods=['POST'])
def api_get_sales_teams():
    uid, m = get_odoo_connection()
    return jsonify({'success': True, 'teams': m.execute_kw(ODOO_DB, uid, ODOO_PASS, 'crm.team', 'search_read', [[('active', '=', True)]], {'fields': ['id', 'name']})})

@app.route('/api/sales_team_report', methods=['POST'])
def api_sales_team_report():
    try:
        data = request.json; start = data.get('start'); end = data.get('end'); tid = int(data.get('team_id')) if data.get('team_id') else None
        uid, m = get_odoo_connection(); cid = get_company_id(uid, m)
        dom_s = [('move_id.state','=','posted'),('move_id.move_type','=','out_invoice'),('date','>=',start),('date','<=',end),('product_id','!=',False)]
        if cid: dom_s.append(('company_id','=',cid))
        lines_s = m.execute_kw(ODOO_DB, uid, ODOO_PASS, 'account.move.line', 'search_read', [dom_s + [('exclude_from_invoice_tab', '=', False)]], {'fields':['product_id','quantity','price_subtotal','move_id']})
        dom_r = [('move_id.state','=','posted'),('move_id.move_type','=','out_refund'),('date','>=',start),('date','<=',end),('product_id','!=',False)]
        if cid: dom_r.append(('company_id','=',cid))
        lines_r = m.execute_kw(ODOO_DB, uid, ODOO_PASS, 'account.move.line', 'search_read', [dom_r + [('exclude_from_invoice_tab', '=', False)]], {'fields':['product_id','quantity','price_subtotal','move_id']})
        move_ids = set([l['move_id'][0] for l in lines_s] + [l['move_id'][0] for l in lines_r])
        move_map = {}
        if move_ids:
            moves = m.execute_kw(ODOO_DB, uid, ODOO_PASS, 'account.move', 'read', [list(move_ids)], {'fields':['team_id']})
            for mov in moves:
                t_id = mov['team_id'][0] if mov['team_id'] else 0
                t_nm = mov['team_id'][1] if mov['team_id'] else "Undefined"
                move_map[mov['id']] = {'id': t_id, 'name': t_nm}
        agg = {}
        def process(lines, is_ret):
            for l in lines:
                mid = l['move_id'][0]; pid = l['product_id'][0]; team_info = move_map.get(mid, {'id':0,'name':'Undefined'})
                if tid and team_info['id'] != tid: continue
                key = (team_info['name'], pid)
                if key not in agg: agg[key] = {'qty': 0.0, 'val': 0.0}
                q = l.get('quantity',0); v = l.get('price_subtotal',0)
                if is_ret: agg[key]['qty'] -= abs(q); agg[key]['val'] -= abs(v)
                else: agg[key]['qty'] += q; agg[key]['val'] += v
        process(lines_s, False); process(lines_r, True)
        pids = list(set([k[1] for k in agg.keys()]))
        prods = []
        if pids: prods = m.execute_kw(ODOO_DB, uid, ODOO_PASS, 'product.product', 'read', [pids], {'fields':['name','default_code','barcode','categ_id','seller_ids']})
        pmap = {p['id']: p for p in prods}
        sids = list(set([p['seller_ids'][0] for p in prods if p['seller_ids']]))
        smap = {}
        if sids:
            sinfos = m.execute_kw(ODOO_DB, uid, ODOO_PASS, 'product.supplierinfo', 'read', [sids], {'fields':['name']})
            for si in sinfos: 
                if si['name']: smap[si['id']] = si['name'][1]
        rep = []
        for (team, pid), stats in agg.items():
            if stats['qty']==0 and stats['val']==0: continue
            p = pmap.get(pid, {})
            cat = p.get('categ_id', ["","Unknown"])[1] if p.get('categ_id') else "Unknown"
            vnd = smap.get(p['seller_ids'][0], "-") if p.get('seller_ids') else "-"
            rep.append({'team': team, 'barcode': p.get('barcode') or '-', 'ref': p.get('default_code') or '-', 'category': cat, 'product': p.get('name', 'Unknown'), 'vendor': vnd, 'qty': stats['qty'], 'amount': stats['val']})
        rep.sort(key=lambda x: (x['team'], x['product']))
        return jsonify({'success': True, 'report': rep})
    except Exception as e: return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/get_customers', methods=['POST'])
def api_get_customers(): return jsonify({'success': False})
@app.route('/api/create_sales_order', methods=['POST'])
def api_create_sales_order(): return jsonify({'success': False})
@app.route('/get_label_files')
def get_label_files():
    try:
        if not os.path.exists(LABELS_DIR):
            os.makedirs(LABELS_DIR)
        files = [f for f in os.listdir(LABELS_DIR) if f.endswith('.btw')]
        return jsonify(files)
    except Exception as e:
        return jsonify([])
@app.route('/print_labels', methods=['POST'])
def print_labels_file():
    try:
        data = request.json
        items = data.get('items', [])
        filename = data.get('filename')
        if not filename: return jsonify({'success': False, 'error': 'No file selected'})
        db_path = os.path.join(LABELS_DIR, 'odoo.txt')
        with open(db_path, 'w') as f:
            for item in items:
                clean_name = item['name'].replace(',', ' ')
                f.write(f"{item['barcode']},{clean_name},{item['qty']}\n")
        btw_path = os.path.join(LABELS_DIR, filename)
        if os.path.exists(btw_path):
            os.startfile(btw_path)
            return jsonify({'success': True})
        else:
            return jsonify({'success': False, 'error': 'Label file not found on server'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
@app.route('/load_products', methods=['POST'])
def load_products():
    try:
        file = request.files['productFile']
        if not file: return jsonify({'success': False, 'error': 'No file uploaded'})
        if file.filename.endswith('.csv'):
            df = pd.read_csv(file)
        else:
            df = pd.read_excel(file)
        df.columns = [c.lower() for c in df.columns]
        products = []
        for _, row in df.iterrows():
            products.append({
                'barcode': str(row.get('barcode', '')).replace('nan', ''),
                'name': str(row.get('name', row.get('product', 'Unknown'))),
                'ref': str(row.get('default_code', row.get('ref', ''))).replace('nan', ''),
                'price': float(row.get('list_price', row.get('price', 0)))
            })
        return jsonify({'success': True, 'products': products})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
@app.route('/fetch_odoo_products', methods=['POST'])
def fetch_odoo_products():
    try:
        uid, m = get_odoo_connection()
        cid = get_company_id(uid, m)
        ctx = get_context(cid)
        products = m.execute_kw(ODOO_DB, uid, ODOO_PASS, 'product.product', 'search_read', [[('active', '=', True)]], {'fields': ['id', 'name', 'default_code', 'barcode', 'list_price'], 'limit': 100000, 'context': ctx})
        result = [{'id': p['id'], 'name': p['name'], 'ref': p['default_code'] or '', 'barcode': p['barcode'] or '', 'price': p['list_price']} for p in products]
        return jsonify({'success': True, 'products': result})
    except Exception as e: return jsonify({'success': False, 'error': str(e)}), 500
@app.route('/parse_text', methods=['POST'])
def parse_text_route(): return jsonify({'success': False})

def open_browser(): webbrowser.open_new('http://localhost:2000')

if __name__ == '__main__':
    try:
        from waitress import serve
        Timer(1, open_browser).start()
        print("Serving on http://0.0.0.0:2000")
        serve(app, host='0.0.0.0', port=2000)
    except Exception as e:
        print(f"Startup Error: {e}")
        input("Press Enter to close...")