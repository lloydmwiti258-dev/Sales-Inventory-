from flask import Flask, jsonify, render_template, request
import pandas as pd
import threading
from collections import defaultdict
from datetime import datetime
from integrated_data import process_data, get_correlations, get_insights, get_pearson_matrix, NEW_PRODUCTS

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

    # New products analytics
    _np_upper = {p.upper() for p in NEW_PRODUCTS}
    def _np_col(d, col):
        return int(d[col].sum()) if col in d.columns else 0

    # Revenue per Bag Type from the Revenue Breakdown sheet
    rev_by_bagtype = {}
    if revenue_df is not None and not revenue_df.empty and 'Bag Type' in revenue_df.columns:
        rev_col = next((c for c in ('Total Revenue', 'TOTAL REVENUE', 'Total') if c in revenue_df.columns), None)
        if rev_col:
            for bt, grp_r in revenue_df.groupby('Bag Type'):
                rev_by_bagtype[str(bt).strip().upper()] = int(grp_r[rev_col].apply(
                    lambda v: float(str(v).replace(',', '') or 0) if not isinstance(v, (int, float)) else v
                ).sum())

    # New products are identified by Bag Type (column D of SALE sheet)
    np_mask = master['Bag Type'].str.strip().str.upper().isin(_np_upper) if 'Bag Type' in master.columns else pd.Series([False] * len(master))
    np_df   = master[np_mask].copy()

    # Per-product pipeline rows for the tab table
    np_data = []
    if not np_df.empty:
        for prod, grp in np_df.groupby('Bag Type'):
            np_data.append({
                'product':        prod,
                'category':       grp['Category'].mode()[0] if 'Category' in grp.columns and not grp.empty else '—',
                'total_sales':    _np_col(grp, 'Total Sales'),
                'total_stock':    _np_col(grp, 'Total Stock'),
                'total_dispatch': _np_col(grp, 'Total Dispatch'),
                'cut_in_store':   _np_col(grp, 'Bags in cut store'),
                'stitching_wip':  _np_col(grp, 'Stitching WIP'),
                'finishing_wip':  _np_col(grp, 'WIP to finishing'),
                'warehouse':      _np_col(grp, 'Total Warehouse Stock'),
                'shop_stores':    _np_col(grp, 'Total Stock'),
                'marketing':      _np_col(grp, 'Total Marketing'),
                'revenue':        rev_by_bagtype.get(prod.strip().upper(), 0),
            })
        np_data.sort(key=lambda x: x['total_sales'], reverse=True)

    # Per-product color breakdown for cascading dropdown
    np_color_data = {}
    if not np_df.empty and 'Color' in np_df.columns:
        for prod, prod_grp in np_df.groupby('Bag Type'):
            color_rows = []
            for color, color_grp in prod_grp.groupby('Color'):
                if not str(color).strip():
                    continue
                color_rows.append({
                    'color':          str(color),
                    'total_sales':    _np_col(color_grp, 'Total Sales'),
                    'total_stock':    _np_col(color_grp, 'Total Stock'),
                    'total_dispatch': _np_col(color_grp, 'Total Dispatch'),
                    'cut_in_store':   _np_col(color_grp, 'Bags in cut store'),
                    'stitching_wip':  _np_col(color_grp, 'Stitching WIP'),
                    'finishing_wip':  _np_col(color_grp, 'WIP to finishing'),
                    'warehouse':      _np_col(color_grp, 'Total Warehouse Stock'),
                    'shop_stores':    _np_col(color_grp, 'Total Stock'),
                    'marketing':      _np_col(color_grp, 'Total Marketing'),
                    'revenue':        0,
                })
            color_rows.sort(key=lambda x: x['total_sales'], reverse=True)
            np_color_data[prod] = color_rows

    # Best product / category / shop / region for new products
    np_shop_analysis = data_dict.get('new_products_shop_analysis', [])

    best_product  = np_data[0] if np_data else None
    np_by_cat     = defaultdict(int)
    for r in np_data:
        np_by_cat[r['category']] += r['total_sales']
    best_cat_name, best_cat_sales = max(np_by_cat.items(), key=lambda x: x[1], default=('—', 0))

    best_shop = next(iter([s for s in np_shop_analysis if s['sales'] > 0]), None)

    np_by_region = defaultdict(int)
    for s in np_shop_analysis:
        np_by_region[s['region']] += s['sales']
    best_region_name, best_region_sales = max(np_by_region.items(), key=lambda x: x[1], default=('—', 0))

    new_products_summary = {
        'total_sales':     _np_col(np_df, 'Total Sales'),
        'total_stock':     _np_col(np_df, 'Total Stock'),
        'total_dispatch':  _np_col(np_df, 'Total Dispatch'),
        'cut_in_store':    _np_col(np_df, 'Bags in cut store'),
        'stitching_wip':   _np_col(np_df, 'Stitching WIP'),
        'finishing_wip':   _np_col(np_df, 'WIP to finishing'),
        'warehouse':       _np_col(np_df, 'Total Warehouse Stock'),
        'best_product':    {'name': best_product['product'],  'sales': best_product['total_sales']}  if best_product  else {'name': '—', 'sales': 0},
        'best_category':   {'name': best_cat_name,            'sales': best_cat_sales},
        'best_shop':       {'name': best_shop['shop'],        'sales': best_shop['sales']}            if best_shop     else {'name': '—', 'sales': 0},
        'best_region':     {'name': best_region_name,         'sales': best_region_sales},
        'shop_breakdown':  np_shop_analysis,
    }

    return jsonify({
        'summary':        summary,
        'master_data':    df.to_dict(orient='records'),
        'correlations':   get_correlations(data_dict),
        'shop_analysis':  shop_analysis,
        'revenue_data':   revenue_data,
        'stitched_data':         stitched_data,
        'new_products_summary':       new_products_summary,
        'new_products_data':          np_data,
        'new_products_color_data':    np_color_data,
        'new_products_shop_detail':   data_dict.get('new_products_shop_detail', []),
        'refreshed':             _cache.get('refreshed'),
        'filters': {
            'categories': sorted(master['Category'].unique().tolist()),
            'bagtypes':   sorted(master['Bag Type'].unique().tolist()),
            'products':   sorted(master['Product Name'].unique().tolist()),
            'colors':     sorted(master['Color'].unique().tolist()),
            'regions':    regions,
            'shops':      shops,
        }
    })

@app.route('/api/debug/products')
def api_debug_products():
    data_dict = get_data()
    if not data_dict:
        return jsonify({'error': 'No data'}), 503
    master = data_dict['master']
    cols = list(master.columns)
    products = sorted(str(v) for v in master['Product Name'].unique()) if 'Product Name' in master.columns else []
    np_upper = {p.upper() for p in NEW_PRODUCTS}
    matched = [p for p in products if p.upper() in np_upper]

    # Also check the raw SALE sheet directly
    from integrated_data import get_client, SHEET_NAME
    try:
        sh = get_client().open(SHEET_NAME)
        ws = sh.worksheet('SALE')
        rows = ws.get_all_values()
        header_row = rows[0] if rows else []
        col_d_values = sorted(set(r[3] for r in rows[1:] if len(r) > 3 and r[3].strip())) if len(header_row) > 3 else []
    except Exception as e:
        header_row = [f'error: {e}']
        col_d_values = []

    return jsonify({
        'master_columns': cols,
        'all_product_names_in_master': products,
        'matched_new_products': matched,
        'sale_sheet_headers': header_row,
        'sale_sheet_col_d_header': header_row[3] if len(header_row) > 3 else '—',
        'sale_sheet_col_d_unique_values': col_d_values[:60],
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
