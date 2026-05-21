from flask import Flask, jsonify, render_template, request
import pandas as pd
import threading
from datetime import datetime
from integrated_data import process_data, get_correlations, get_insights, get_pearson_matrix

app = Flask(__name__)
app.config['TEMPLATES_AUTO_RELOAD'] = True

_cache = {}

def load_data():
    try:
        data_dict = process_data()
        _cache['data_dict'] = data_dict
        _cache['refreshed'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        _cache['error']     = None
        print("Integrated cache refreshed at", _cache['refreshed'])
    except Exception as e:
        _cache['error'] = str(e)
        print("[WARNING] Could not load integrated data:", e)

def get_data():
    if 'data_dict' not in _cache:
        load_data()
    if _cache.get('error'):
        return None
    return _cache['data_dict']

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/integrated-dashboard')
def api_integrated_dashboard():
    data_dict = get_data()
    if not data_dict:
        return jsonify({'error': _cache.get('error', 'Data not loaded')}), 503
    
    master = data_dict['master']
    
    # Filtering
    cat = request.args.get('category')
    bt = request.args.get('bagtype')
    prod = request.args.get('product')
    color = request.args.get('color')
    
    df = master.copy()
    if cat and cat != 'All': df = df[df['Category'] == cat]
    if bt and bt != 'All': df = df[df['Bag Type'] == bt]
    if prod and prod != 'All': df = df[df['Product Name'] == prod]
    if color and color != 'All': df = df[df['Color'] == color]
    
    # KPI Calculations from filtered master
    def col_sum(col):
        return int(df[col].sum()) if col in df.columns else 0

    # Monthly target KPIs come directly from target_df to avoid inflation.
    # (target_summary is at Category/Bag Type level; merging into full master multiplies values.)
    target_df = data_dict.get('target_df', pd.DataFrame())
    tdf = target_df.copy() if not target_df.empty else pd.DataFrame()
    if not tdf.empty:
        if cat and cat != 'All' and 'Category' in tdf.columns:
            tdf = tdf[tdf['Category'] == cat]
        if bt and bt != 'All' and 'Bag Type' in tdf.columns:
            tdf = tdf[tdf['Bag Type'] == bt]

    def tgt_sum(col):
        return int(tdf[col].sum()) if not tdf.empty and col in tdf.columns else 0

    summary = {
        'total_sales': col_sum('Total Sales'),
        'total_stock': col_sum('Total Stock'),
        'total_dispatch': col_sum('Total Dispatch'),
        'warehouse_availability': col_sum('Total Warehouse Stock'),
        'warehouse_store':        col_sum('Warehouse Store'),
        'ktda_main_store':        col_sum('KTDA MAIN STORE'),
        'marketing_totals':   col_sum('Total Marketing'),
        'marketing_kenya':    col_sum('Marketing Kenya'),
        'marketing_sinza':    col_sum('Marketing Sinza'),
        'marketing_uganda':   col_sum('Marketing Uganda'),
        # Production breakdown
        'cut_in_store':     col_sum('Bags in cut store'),
        'stitching_wip':    col_sum('Stitching WIP'),
        'weekly_issued':    col_sum('Bags issued for stitching'),
        'weekly_stitched':  col_sum('Bags stitched'),
        'weekly_finished':  col_sum('Bags finished'),
        'finishing_wip':    col_sum('WIP to finishing'),
        # Monthly target — read directly from target sheet (not inflated master)
        'target_sales': tgt_sum('Monthly sales target'),
        'actual_sales_target': tgt_sum('Actual sales'),
        'total_deficit': tgt_sum('Deficit'),
    }
    summary['target_achievement'] = round((summary['actual_sales_target'] / summary['target_sales'] * 100), 2) if summary['target_sales'] > 0 else 0
    
    shop_analysis = data_dict.get('shop_analysis', [])
    regions = sorted(set(s['region'] for s in shop_analysis if s['region'] != 'Other'))
    shops   = [s['shop'] for s in shop_analysis]

    # Revenue breakdown
    revenue_df = data_dict.get('revenue_df', None)
    revenue_data = []
    if revenue_df is not None and not revenue_df.empty:
        revenue_df_clean = revenue_df.copy()
        for c in revenue_df_clean.select_dtypes(include=['float64','float32']).columns:
            revenue_df_clean[c] = revenue_df_clean[c].fillna(0).round(0).astype(int)
        revenue_data = revenue_df_clean.to_dict(orient='records')

    # Stitched complexity breakdown
    stitched_df = data_dict.get('stitched_df', None)
    stitched_data = []
    if stitched_df is not None and not stitched_df.empty:
        stitched_clean = stitched_df.copy()
        for c in stitched_clean.select_dtypes(include=['float64','float32']).columns:
            stitched_clean[c] = stitched_clean[c].fillna(0).round(0).astype(int)
        stitched_data = stitched_clean.to_dict(orient='records')

    return jsonify({
        'summary':        summary,
        'master_data':    df.to_dict(orient='records'),
        'correlations':   get_correlations(data_dict),
        'shop_analysis':  shop_analysis,
        'revenue_data':   revenue_data,
        'stitched_data':  stitched_data,
        'refreshed':      _cache.get('refreshed'),
        'filters': {
            'categories': sorted(master['Category'].unique().tolist()),
            'bagtypes':   sorted(master['Bag Type'].unique().tolist()),
            'products':   sorted(master['Product Name'].unique().tolist()),
            'colors':     sorted(master['Color'].unique().tolist()),
            'regions':    regions,
            'shops':      shops,
        }
    })

@app.route('/api/insights')
def api_get_insights():
    data_dict = get_data()
    if not data_dict:
        return jsonify({'error': 'Data not loaded'}), 503
    return jsonify(get_insights(data_dict))

@app.route('/api/refresh')
def api_refresh():
    load_data()
    if _cache.get('error'):
        return jsonify({'status': 'error', 'message': _cache['error']}), 503
    return jsonify({'status': 'ok', 'refreshed': _cache.get('refreshed')})

@app.route('/api/correlation-matrix')
def api_correlation_matrix():
    data_dict = get_data()
    if not data_dict:
        return jsonify({'error': 'Data not loaded'}), 503
    return jsonify(get_pearson_matrix(data_dict))

if __name__ == '__main__':
    print("Denri Africa Operational Dashboard -> http://localhost:5002")
    threading.Thread(target=load_data, daemon=True).start()
    app.run(debug=True, port=5002)
