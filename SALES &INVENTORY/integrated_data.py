import gspread
from google.oauth2.service_account import Credentials
import pandas as pd
import numpy as np
import os
import json
from dotenv import load_dotenv

load_dotenv()

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════
SHEET_NAME = os.getenv('GOOGLE_SHEET_NAME', 'SALES & INVENTORY DASHBOARD')

SHOP_REGIONS = {
    'Hazina':    'Nairobi CBD',
    'Hilton':    'Nairobi CBD',
    'Starmall':  'Nairobi CBD',
    'Ktda':      'Nairobi CBD',
    'Mombasa':   'Coastal Region',
    'Kakamega':  'Western & Nyanza',
    'Kisumu':    'Western & Nyanza',
    'Kisii':     'Western & Nyanza',
    'Busia':     'Western & Nyanza',
    'Meru':      'Central Region',
    'Nanyuki':   'Central Region',
    'Thika':     'Central Region',
    'Eldoret':   'Rift Valley',
    'Nakuru':    'Rift Valley',
    'Kitengela': 'Rift Valley',
    'Sinza':     'Diaspora',
    'Tanzania':  'Diaspora',
    'Uganda':    'Diaspora',
    'Website':   'Online',
    'Rongai':    'Rift Valley',
}

SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive',
]

import traceback
from requests.adapters import HTTPAdapter

class _TimeoutAdapter(HTTPAdapter):
    """Force a 30-second timeout on every Google API request."""
    def send(self, *args, **kwargs):
        kwargs.setdefault('timeout', 30)
        return super().send(*args, **kwargs)

def get_client():
    try:
        creds_json = os.getenv('GOOGLE_CREDENTIALS_JSON')
        if not creds_json:
            raise ValueError("GOOGLE_CREDENTIALS_JSON environment variable is not set")
        creds_dict = json.loads(creds_json)
        creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
        client = gspread.authorize(creds)
        adapter = _TimeoutAdapter()
        client.http_client.session.mount('https://', adapter)
        client.http_client.session.mount('http://', adapter)
        return client
    except Exception as e:
        print(f"Auth Error: {e}")
        raise e

def fetch_sheet_as_df(client, sheet_name, worksheet_name):
    try:
        sh = client.open(sheet_name)
        try:
            ws = sh.worksheet(worksheet_name)
        except gspread.exceptions.WorksheetNotFound:
            # Try to find a case-insensitive match or similar name
            available = [w.title for w in sh.worksheets()]
            print(f"[ERROR] Worksheet '{worksheet_name}' not found. Available: {available}")
            # Try case-insensitive match
            for title in available:
                if title.strip().upper() == worksheet_name.upper():
                    print(f"[INFO] Using '{title}' instead of '{worksheet_name}'")
                    ws = sh.worksheet(title)
                    break
            else:
                return pd.DataFrame()
                
        data = ws.get_all_values()
        if not data:
            return pd.DataFrame()
        df = pd.DataFrame(data[1:], columns=data[0])
        return df
    except Exception as e:
        print(f"Error fetching {worksheet_name}: {e}")
        return pd.DataFrame()

def clean_numeric(val):
    if isinstance(val, str):
        val = val.replace(',', '').replace(' ', '').replace('$', '').strip()
        if not val or val == '-': return 0
        try:
            return float(val)
        except ValueError:
            return 0
    return val if isinstance(val, (int, float)) else 0

def process_data():
    try:
        return _process_data_impl()
    except Exception as e:
        print("Data processing error:")
        print(traceback.format_exc())
        raise e

import concurrent.futures

# Maps lowercase variants -> canonical column names
_COL_ALIASES = {
    'category': 'Category',
    'color': 'Color',
    'colour': 'Color',
    'product name': 'Product Name',
    'product': 'Product Name',
    'bag type': 'Bag Type',
    'bag_type': 'Bag Type',
    'bagtype': 'Bag Type',
    # MONTHLY TARGET sheet
    'target': 'Monthly sales target',
    'sales': 'Actual sales',
    'monthly sales target': 'Monthly sales target',
    'actual sales': 'Actual sales',
    'deficit': 'Deficit',
    # Computed totals
    'total sales': 'Total Sales',
    'total stock': 'Total Stock',
    'total dispatch': 'Total Dispatch',
    'dispatch': 'Total Dispatch',
    'total marketing': 'Total Marketing',
    # PRODUCTION sheet
    'cut in store': 'Bags in cut store',
    'issued': 'Bags issued for stitching',
    'bags in cut store': 'Bags in cut store',
    'bags issued for stitching': 'Bags issued for stitching',
    'stitching wip': 'Stitching WIP',
    'complexity': 'Complexity',
    # WAREHOUSE sheet
    'warehouse store': 'Warehouse Store',
    'wip': 'WIP to finishing',
    'finishing wip': 'WIP to finishing',
    'stitched': 'Bags stitched',
    'finished': 'Bags finished',
    'wip to finishing': 'WIP to finishing',
    'bags stitched': 'Bags stitched',
    'bags finished': 'Bags finished',
    'finished stock': 'Finished stock',
    # STOCKS sheet
    'ktda main store': 'KTDA MAIN STORE',
}

def _normalize_df_columns(df):
    raw_uppers = {c.strip().upper() for c in df.columns}
    # SALE/STOCKS/DISPATCH have both COLOR (short code) and COLOUR (full name).
    # COLOUR is the authoritative full color name; drop the short COLOR in those sheets.
    prefer_colour = 'COLOR' in raw_uppers and 'COLOUR' in raw_uppers

    col_map = {}
    for c in df.columns:
        alias = c.strip().lower()
        if alias == 'color' and prefer_colour:
            col_map[c] = '_drop_short_color'
        elif alias in _COL_ALIASES and c != _COL_ALIASES[alias]:
            col_map[c] = _COL_ALIASES[alias]
    if col_map:
        df.rename(columns=col_map, inplace=True)
    if '_drop_short_color' in df.columns:
        df.drop(columns=['_drop_short_color'], inplace=True)
    return df

_KNOWN_HEADERS = {
    'category', 'colour', 'color', 'product name', 'product',
    'bag type', 'bag_type', 'bagtype', 'total sales', 'total stock',
    'monthly sales target', 'actual sales', 'deficit', 'bags stitched',
    'dispatch', 'total dispatch', 'total marketing', 'finished stock',
    'bags finished', 'wip to finishing', 'stitching wip',
    'cut in store', 'issued', 'warehouse store', 'target', 'sales',
}

def _find_header_row(data):
    """Return the index of the row most likely to be the header (contains known col names)."""
    best_idx, best_score = 0, 0
    for i, row in enumerate(data):
        score = sum(1 for c in row if c.strip().lower() in _KNOWN_HEADERS)
        if score > best_score:
            best_score, best_idx = score, i
        if best_score >= 2:
            break
    # Only trust the detected row if it has at least 1 known header
    return best_idx if best_score >= 1 else None

def fetch_ws_data(sh, ws_name):
    try:
        ws = sh.worksheet(ws_name)
    except gspread.exceptions.WorksheetNotFound:
        available = [w.title for w in sh.worksheets()]
        for title in available:
            if title.strip().upper() == ws_name.upper():
                ws = sh.worksheet(title)
                break
        else:
            print(f"[ERROR] Worksheet '{ws_name}' not found.")
            return ws_name, pd.DataFrame()

    data = ws.get_all_values()
    if not data:
        return ws_name, pd.DataFrame()

    header_idx = _find_header_row(data)
    if header_idx is None or header_idx + 1 >= len(data):
        print(f"[WARNING] '{ws_name}' has no recognisable header row. Actual row 0: {data[0][:6]}")
        return ws_name, pd.DataFrame()

    if header_idx > 0:
        print(f"[INFO] '{ws_name}' header found at row {header_idx} (skipped {header_idx} title rows)")

    headers = [c.strip() for c in data[header_idx]]
    rows = data[header_idx + 1:]
    df = pd.DataFrame(rows, columns=headers)
    df = df.loc[:, df.columns != '']
    _normalize_df_columns(df)
    if df.columns.duplicated().any():
        dupes = df.columns[df.columns.duplicated()].tolist()
        print(f"[WARNING] '{ws_name}' duplicate columns after normalisation: {dupes} — keeping first")
        df = df.loc[:, ~df.columns.duplicated()]
    # Drop summary / total rows (rows where Category is blank or a grand-total marker)
    if 'Category' in df.columns:
        drop_vals = {'', 'total', 'totals', 'grand total', 'subtotal'}
        df = df[~df['Category'].str.strip().str.lower().isin(drop_vals)].reset_index(drop=True)
    return ws_name, df

def _smart_merge(left, right, keys, how='outer'):
    """Merge only on keys present in both frames."""
    common = [k for k in keys if k in left.columns and k in right.columns]
    if not common or right.empty:
        return left
    return left.merge(right, on=common, how=how)

def _fetch_revenue_breakdown(sh):
    """Fetch REVENUE BREAKDOWN sheet.
    Structure: Col B = Category, Col C = Bag Type, Cols D:V = shop revenues, Col W = Total Revenue.
    Tries sheet names containing 'revenue' (case-insensitive).
    """
    ws = None
    available = [w.title for w in sh.worksheets()]
    for title in available:
        if 'revenue' in title.lower():
            ws = sh.worksheet(title)
            print(f"[REVENUE] Using sheet '{title}'")
            break
    if ws is None:
        print(f"[REVENUE] No revenue sheet found. Available: {available}")
        return pd.DataFrame()

    data = ws.get_all_values()
    if not data:
        return pd.DataFrame()

    # Find header row: row where col B and col D are both non-numeric text
    header_idx = None
    for i, row in enumerate(data):
        if len(row) < 4:
            continue
        b = row[1].strip()
        d = row[3].strip() if len(row) > 3 else ''
        if b and d and not _is_num(b) and not _is_num(d):
            header_idx = i
            break

    if header_idx is None:
        print("[REVENUE] Could not detect header row, using row 0")
        header_idx = 0

    headers = [c.strip() for c in data[header_idx]]
    # Col B (idx 1) = Category, Col C (idx 2) = Bag Type
    # Shop columns: D (idx 3) through V (idx 21)
    shop_cols = {}
    for idx in range(3, min(22, len(headers))):
        name = headers[idx]
        if name:
            shop_cols[idx] = name
    total_idx = 22  # Column W

    records = []
    last_category = ''
    for row in data[header_idx + 1:]:
        if len(row) < 3:
            continue
        category = row[1].strip() if len(row) > 1 else ''
        bag_type = row[2].strip() if len(row) > 2 else ''

        # Skip pure total/blank rows
        skip_vals = {'total', 'grand total', 'subtotal', ''}
        if category.lower() in skip_vals and bag_type.lower() in skip_vals:
            continue

        # Carry forward category if only bag type changes (merged cells pattern)
        if category:
            last_category = category
        else:
            category = last_category

        if not bag_type:
            continue  # need at least a bag type

        rec = {'Category': category, 'Bag Type': bag_type}
        for idx, col_name in shop_cols.items():
            rec[col_name] = clean_numeric(row[idx]) if idx < len(row) else 0
        rec['Total Revenue'] = clean_numeric(row[total_idx]) if total_idx < len(row) else sum(
            clean_numeric(row[i]) for i in shop_cols if i < len(row))
        records.append(rec)

    if not records:
        print("[REVENUE] Sheet parsed but no data rows found")
        return pd.DataFrame()

    df = pd.DataFrame(records)
    print(f"[REVENUE] Loaded {len(df)} rows, shops: {list(shop_cols.values())}")
    return df

def _is_num(s):
    try:
        float(str(s).replace(',', ''))
        return True
    except (ValueError, TypeError):
        return False


def _process_data_impl():
    client = get_client()
    sh = client.open(SHEET_NAME)

    # Parallel Fetching
    sheets = ['SALE', 'MARKETING', 'STOCKS', 'PRODUCTION', 'WAREHOUSE', 'DISPATCH', 'MONTHLY TARGET']
    dfs = {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(sheets)) as executor:
        future_to_ws = {executor.submit(fetch_ws_data, sh, s): s for s in sheets}
        for future in concurrent.futures.as_completed(future_to_ws):
            name, df = future.result()
            dfs[name] = df

    # Define standard keys
    merge_keys = ['Category', 'Color', 'Product Name', 'Bag Type']
    
    def agg_df(df, keys):
        if df.empty: return df
        actual_keys = [k for k in keys if k in df.columns]
        if not actual_keys: return df
        numeric_cols = [c for c in df.columns if c not in keys]
        return df.groupby(actual_keys, as_index=False)[numeric_cols].sum()

    # Process each sheet
    # Sales — use the TOTAL column directly (do NOT re-sum shop columns)
    sales_df = dfs.get('SALE', pd.DataFrame())
    if sales_df.empty:
        sales_df = pd.DataFrame(columns=merge_keys + ['Total Sales'])
    else:
        sales_val_cols = [c for c in sales_df.columns if c not in merge_keys]
        for col in sales_val_cols: sales_df[col] = sales_df[col].apply(clean_numeric)
        if 'Total Sales' not in sales_df.columns:
            if 'TOTAL' in sales_df.columns:
                sales_df.rename(columns={'TOTAL': 'Total Sales'}, inplace=True)
            else:
                shop_cols = [c for c in sales_val_cols if c.lower() not in ('total', 'total sales')]
                sales_df['Total Sales'] = sales_df[shop_cols].sum(axis=1)
    sales_df = agg_df(sales_df, merge_keys)

    # Marketing — sum all non-key columns (KENYA, SINZA, UGANDA etc.)
    mkt_df = dfs.get('MARKETING', pd.DataFrame())
    if mkt_df.empty:
        mkt_df = pd.DataFrame(columns=merge_keys + ['Total Marketing', 'Marketing Kenya', 'Marketing Sinza', 'Marketing Uganda'])
    else:
        # Rename Kenya/Sinza/Uganda cols before numeric conversion so we can keep them separately
        _mkt_post_map = {}
        for col in mkt_df.columns:
            cl = col.strip().lower()
            if cl == 'kenya':   _mkt_post_map[col] = 'Marketing Kenya'
            elif cl == 'sinza': _mkt_post_map[col] = 'Marketing Sinza'
            elif cl == 'uganda': _mkt_post_map[col] = 'Marketing Uganda'
        if _mkt_post_map:
            mkt_df.rename(columns=_mkt_post_map, inplace=True)
            print(f"[MARKETING] Post columns found: {list(_mkt_post_map.values())}")
        mkt_val_cols = [c for c in mkt_df.columns if c not in merge_keys]
        for col in mkt_val_cols:
            mkt_df[col] = mkt_df[col].apply(clean_numeric)
        if 'Total Marketing' not in mkt_df.columns:
            mkt_df['Total Marketing'] = mkt_df[mkt_val_cols].sum(axis=1)
    mkt_df = agg_df(mkt_df, merge_keys)

    # Stocks — Total Stock = shops (STARMALL:RONGAI), KTDA MAIN STORE kept separate for warehouse
    stocks_df = dfs.get('STOCKS', pd.DataFrame())
    if stocks_df.empty:
        stocks_df = pd.DataFrame(columns=merge_keys + ['Total Stock', 'KTDA MAIN STORE'])
    else:
        stocks_val_cols = [c for c in stocks_df.columns if c not in merge_keys]
        for col in stocks_val_cols: stocks_df[col] = stocks_df[col].apply(clean_numeric)
        if 'Total Stock' not in stocks_df.columns:
            exclude = {'total', 'total stock', 'ktda main store'}
            shop_cols = [c for c in stocks_val_cols if c.lower() not in exclude]
            stocks_df['Total Stock'] = stocks_df[shop_cols].sum(axis=1)
    stocks_df = agg_df(stocks_df, merge_keys)

    # Production
    prod_df = dfs.get('PRODUCTION', pd.DataFrame())
    if prod_df.empty:
        prod_df = pd.DataFrame(columns=merge_keys + ['Bags in cut store', 'Bags issued for stitching', 'Stitching WIP'])
    else:
        for col in prod_df.columns:
            if col not in merge_keys:
                prod_df[col] = prod_df[col].apply(clean_numeric)
    prod_df = agg_df(prod_df, merge_keys)

    # Warehouse
    wh_df = dfs.get('WAREHOUSE', pd.DataFrame())
    stitched_df = pd.DataFrame()
    if wh_df.empty:
        wh_df = pd.DataFrame(columns=merge_keys + ['Finished stock', 'WIP to finishing', 'Bags stitched', 'Bags finished'])
    else:
        # Extract complexity summary BEFORE clean_numeric converts text → 0
        if 'Complexity' in wh_df.columns and 'Bags stitched' in wh_df.columns:
            cx = wh_df[['Category', 'Bag Type', 'Complexity', 'Bags stitched']].copy() \
                 if 'Category' in wh_df.columns else wh_df[['Bag Type', 'Complexity', 'Bags stitched']].copy()
            cx['Bags stitched'] = cx['Bags stitched'].apply(clean_numeric)
            cx = cx[cx['Complexity'].astype(str).str.strip() != '']
            grp_cx = [c for c in ['Category', 'Bag Type', 'Complexity'] if c in cx.columns]
            if grp_cx:
                stitched_df = cx.groupby(grp_cx, as_index=False)['Bags stitched'].sum()
                print(f"[STITCHED] {len(stitched_df)} rows, complexities: {stitched_df['Complexity'].unique().tolist()}")

        for col in wh_df.columns:
            if col not in merge_keys and col != 'Complexity':
                wh_df[col] = wh_df[col].apply(clean_numeric)
    wh_df = agg_df(wh_df, merge_keys)

    # Dispatch
    disp_df = dfs.get('DISPATCH', pd.DataFrame())
    if disp_df.empty:
        disp_df = pd.DataFrame(columns=merge_keys + ['Total Dispatch'])
    else:
        disp_val_cols = [c for c in disp_df.columns if c not in merge_keys]
        for col in disp_val_cols:
            disp_df[col] = disp_df[col].apply(clean_numeric)
        if 'Total Dispatch' not in disp_df.columns:
            # Prefer an explicit TOTAL column from the sheet over re-summing
            total_col = next((c for c in disp_val_cols if c.strip().upper() == 'TOTAL'), None)
            if total_col:
                disp_df.rename(columns={total_col: 'Total Dispatch'}, inplace=True)
                print(f"[DISPATCH] Using existing TOTAL column as Total Dispatch")
            else:
                # Fall back: sum all individual shop columns (exclude any 'total' variants)
                disp_shop_cols = [c for c in disp_val_cols if 'total' not in c.lower()]
                disp_df['Total Dispatch'] = disp_df[disp_shop_cols].sum(axis=1)
                print(f"[DISPATCH] Summed {len(disp_shop_cols)} shop columns: {disp_shop_cols}")
        else:
            print(f"[DISPATCH] Total Dispatch column found directly in sheet")
    disp_df = agg_df(disp_df, merge_keys)

    # Monthly Target
    target_df = dfs.get('MONTHLY TARGET', pd.DataFrame())
    if target_df.empty:
        target_df = pd.DataFrame(columns=['Category', 'Bag Type', 'Monthly sales target', 'Actual sales', 'Deficit'])
    else:
        str_cols = {'Category', 'Bag Type'}
        for col in target_df.columns:
            if col not in str_cols:
                target_df[col] = target_df[col].apply(clean_numeric)

    grp_keys = [k for k in ['Category', 'Bag Type'] if k in target_df.columns]
    agg_cols = {c: 'sum' for c in ['Monthly sales target', 'Actual sales', 'Deficit'] if c in target_df.columns}
    if grp_keys and agg_cols:
        target_summary = target_df.groupby(grp_keys, as_index=False).agg(agg_cols).reset_index(drop=True)
        for c in ['Category', 'Bag Type', 'Monthly sales target', 'Actual sales', 'Deficit']:
            if c not in target_summary.columns:
                target_summary[c] = 0
    else:
        print(f"[WARNING] MONTHLY TARGET columns found: {list(target_df.columns)}")
        target_summary = pd.DataFrame(columns=['Category', 'Bag Type', 'Monthly sales target', 'Actual sales', 'Deficit'])

    # ── Per-shop analysis (computed before slimming so shop columns are still available) ──
    _exclude_from_shops = set(merge_keys) | {'Total Sales', 'Total Stock', 'KTDA MAIN STORE', 'Total'}
    sale_shop_cols   = [c for c in sales_df.columns  if c not in _exclude_from_shops and c.upper() != 'TOTAL']
    stocks_shop_cols = [c for c in stocks_df.columns if c not in _exclude_from_shops and c.upper() != 'TOTAL']
    all_shop_cols    = list(dict.fromkeys(sale_shop_cols + [c for c in stocks_shop_cols if c not in sale_shop_cols]))

    shop_analysis = []
    for col in all_shop_cols:
        shop_name = col.title()
        region    = next((v for k, v in SHOP_REGIONS.items() if k.upper() == col.upper()), 'Other')
        shop_analysis.append({
            'shop':    shop_name,
            'region':  region,
            'sales':   int(sales_df[col].sum())  if col in sales_df.columns  else 0,
            'stocks':  int(stocks_df[col].sum()) if col in stocks_df.columns else 0,
        })
    shop_analysis.sort(key=lambda x: x['sales'], reverse=True)

    def slim(df, keys, keep_cols):
        """Keep only merge keys + specific columns to avoid duplicate column conflicts."""
        cols = [c for c in keys + keep_cols if c in df.columns]
        return df[cols].copy() if cols else df

    # --- MERGING ---
    master = slim(sales_df, merge_keys, ['Total Sales'])
    master = _smart_merge(master, slim(mkt_df, merge_keys, ['Total Marketing', 'Marketing Kenya', 'Marketing Sinza', 'Marketing Uganda']), merge_keys)
    master = _smart_merge(master, slim(stocks_df, merge_keys, ['Total Stock', 'KTDA MAIN STORE']), merge_keys)
    master = _smart_merge(master, slim(prod_df, merge_keys, ['Bags in cut store', 'Bags issued for stitching', 'Stitching WIP']), merge_keys)
    master = _smart_merge(master, slim(wh_df, merge_keys, ['Warehouse Store', 'WIP to finishing', 'Bags stitched', 'Bags finished']), merge_keys)
    master = _smart_merge(master, slim(disp_df, merge_keys, ['Total Dispatch']), merge_keys)

    # Merge targets by Category/Bag Type
    master = _smart_merge(master, slim(target_summary, ['Category', 'Bag Type'], ['Monthly sales target', 'Actual sales', 'Deficit']), ['Category', 'Bag Type'], how='left')
    master.fillna(0, inplace=True)

    # Total Warehouse Stock = Warehouse Store (WAREHOUSE sheet) + KTDA MAIN STORE (STOCKS sheet)
    ws_col = master['Warehouse Store'] if 'Warehouse Store' in master.columns else 0
    ktda_col = master['KTDA MAIN STORE'] if 'KTDA MAIN STORE' in master.columns else 0
    master['Total Warehouse Stock'] = ws_col + ktda_col

    # Revenue Breakdown sheet (fetched separately — different structure)
    revenue_df = _fetch_revenue_breakdown(sh)

    return {
        'master':        master,
        'target_df':     target_df,
        'shop_analysis': shop_analysis,
        'revenue_df':    revenue_df,
        'stitched_df':   stitched_df,
    }

def _safe_groupby_agg(master, group_keys, wanted_cols):
    actual_keys = [k for k in group_keys if k in master.columns]
    if not actual_keys:
        empty = pd.DataFrame(columns=group_keys + list(wanted_cols))
        return empty
    valid = {c: 'sum' for c in wanted_cols if c in master.columns}
    result = master.groupby(actual_keys, as_index=False).agg(valid).reset_index(drop=True)
    for c in list(group_keys) + list(wanted_cols):
        if c not in result.columns:
            result[c] = 0
    return result

def get_correlations(data_dict):
    master     = data_dict['master']
    target_df  = data_dict.get('target_df', pd.DataFrame())
    grp        = ['Category', 'Bag Type']

    # Accurate per-category deficit from target_df (master's Deficit is inflated by join)
    cat_deficit = pd.DataFrame()
    if not target_df.empty and 'Deficit' in target_df.columns:
        tgt_keys = [k for k in grp if k in target_df.columns]
        if tgt_keys:
            cat_deficit = target_df.groupby(tgt_keys, as_index=False)['Deficit'].sum()

    def _with_deficit(df):
        if cat_deficit.empty:
            df['Deficit'] = 0
            return df
        keys = [k for k in grp if k in df.columns and k in cat_deficit.columns]
        if not keys:
            df['Deficit'] = 0
            return df
        merged = df.merge(cat_deficit, on=keys, how='left', suffixes=('', '_tgt'))
        deficit_col = 'Deficit_tgt' if 'Deficit_tgt' in merged.columns else 'Deficit'
        df = merged.copy()
        if 'Deficit_tgt' in df.columns:
            df['Deficit'] = df['Deficit_tgt'].fillna(0)
            df.drop(columns=['Deficit_tgt'], inplace=True)
        else:
            df['Deficit'] = df['Deficit'].fillna(0)
        return df

    # 1. Sales, Stocks, Warehouse (incl. KTDA), Production (Stitching WIP), Deficit
    corr1 = _safe_groupby_agg(master, grp, ['Total Sales', 'Total Stock', 'Total Warehouse Stock', 'Stitching WIP'])
    corr1 = _with_deficit(corr1)

    # 2. Sales, Dispatch, Stitching (Bags stitched), Deficit
    corr2 = _safe_groupby_agg(master, grp, ['Total Sales', 'Total Dispatch', 'Bags stitched'])
    corr2 = _with_deficit(corr2)

    # 3. Sales, Marketing, Stocks, Deficit
    corr3 = _safe_groupby_agg(master, grp, ['Total Sales', 'Total Marketing', 'Total Stock'])
    corr3 = _with_deficit(corr3)

    # 4. Monthly Target, Monthly Sales, Deficit — directly from target_df
    if not target_df.empty:
        tgt_keys = [k for k in grp if k in target_df.columns]
        corr4 = _safe_groupby_agg(target_df, tgt_keys, ['Monthly sales target', 'Actual sales', 'Deficit'])
    else:
        corr4 = pd.DataFrame(columns=grp + ['Monthly sales target', 'Actual sales', 'Deficit'])

    # corr1/2/3 — return ALL rows so client-side byCategory() sees every category
    def _sort_all(df, col='Total Sales'):
        if col in df.columns:
            return df.sort_values(col, ascending=False)
        return df

    # corr4 — limit to top 15 target rows (monthly target sheet is already compact)
    def _top(df, col='Monthly sales target', n=15):
        if col in df.columns:
            return df[df[col] > 0].nlargest(n, col)
        return df.head(n)

    return {
        'one':   _sort_all(corr1).to_dict(orient='records'),
        'two':   _sort_all(corr2).to_dict(orient='records'),
        'three': _sort_all(corr3).to_dict(orient='records'),
        'four':  corr4.to_dict(orient='records'),
    }

def get_insights(data_dict):
    import calendar as _cal
    from datetime import datetime
    master     = data_dict['master']
    target_df  = data_dict.get('target_df', pd.DataFrame())
    revenue_df = data_dict.get('revenue_df', pd.DataFrame())

    def col(name):
        return master[name] if name in master.columns else pd.Series(0, index=master.index)

    def safe_int(v):
        try: return int(v) if not pd.isna(v) else 0
        except: return 0

    # ── 1. ALERTS ─────────────────────────────────────────────────────
    alerts = []

    critical = master[(col('Total Sales') > 50) & (col('Total Stock') < 10)]
    if not critical.empty:
        bt = critical['Bag Type'].iloc[0] if 'Bag Type' in critical.columns else 'Unknown'
        alerts.append({'type': 'danger', 'title': 'CRITICAL: Stockout Imminent',
            'message': f"{len(critical)} high-demand bag type(s) like '{bt}' are nearly out of stock."})

    bad_roi = master[(col('Total Marketing') > 20) & (col('Total Sales') < 5)]
    if not bad_roi.empty:
        bt = bad_roi['Bag Type'].iloc[0] if 'Bag Type' in bad_roi.columns else 'Unknown'
        alerts.append({'type': 'warning', 'title': 'Marketing Inefficiency',
            'message': f"High marketing spend on '{bt}' is not converting to sales. Review creative content."})

    if 'Stitching WIP' in master.columns and 'Bags finished' in master.columns:
        if not master[master['Stitching WIP'] > master['Bags finished'] * 2].empty:
            alerts.append({'type': 'info', 'title': 'Production Bottleneck',
                'message': "Stitching WIP is accumulating faster than finishing. Possible labor or materials shortage."})

    if not alerts:
        alerts.append({'type': 'success', 'title': 'All Clear',
            'message': 'No critical operational alerts at this time.'})

    # ── 2. MONTHLY TARGET PROGRESS ────────────────────────────────────
    target_insight = {}
    if not target_df.empty and 'Monthly sales target' in target_df.columns and 'Actual sales' in target_df.columns:
        total_target = safe_int(target_df['Monthly sales target'].sum())
        actual_sales = safe_int(target_df['Actual sales'].sum())
        deficit      = safe_int(target_df['Deficit'].sum()) if 'Deficit' in target_df.columns else max(0, total_target - actual_sales)
        pct          = round(actual_sales / total_target * 100, 1) if total_target > 0 else 0

        today         = datetime.now()
        dom           = today.day
        days_in_month = _cal.monthrange(today.year, today.month)[1]
        days_left     = max(1, days_in_month - dom)
        daily_rate    = round(actual_sales / dom) if dom > 0 else 0
        projected     = round(daily_rate * days_in_month)
        proj_pct      = round(projected / total_target * 100, 1) if total_target > 0 else 0
        need_daily    = round(deficit / days_left)

        status = 'on_track' if projected >= total_target else ('at_risk' if projected >= total_target * 0.85 else 'behind')

        top_deficit = []
        if 'Deficit' in target_df.columns:
            tgt_keys = [k for k in ['Category', 'Bag Type'] if k in target_df.columns]
            if tgt_keys:
                cd = target_df.groupby(tgt_keys, as_index=False).agg(
                    {'Deficit': 'sum', 'Monthly sales target': 'sum', 'Actual sales': 'sum'})
                top_deficit = cd[cd['Deficit'] > 0].sort_values('Deficit', ascending=False).head(3).to_dict(orient='records')

        target_insight = {
            'total_target': total_target, 'actual_sales': actual_sales, 'deficit': deficit,
            'pct_achieved': pct, 'day_of_month': dom, 'days_in_month': days_in_month,
            'days_remaining': days_left, 'daily_run_rate': daily_rate,
            'projected_month_end': projected, 'projected_pct': proj_pct,
            'needed_daily_to_close': need_daily, 'status': status, 'top_deficit': top_deficit,
        }

    # ── 3. SALES vs STOCK ALIGNMENT ───────────────────────────────────
    stock_alignment = {}
    if 'Total Sales' in master.columns and 'Total Stock' in master.columns:
        grp = [k for k in ['Category', 'Bag Type'] if k in master.columns]
        if grp:
            agg = master.groupby(grp, as_index=False).agg({'Total Sales': 'sum', 'Total Stock': 'sum'})
            agg['coverage'] = agg.apply(
                lambda r: round(float(r['Total Stock'] / r['Total Sales']), 2) if r['Total Sales'] > 0 else 999.0, axis=1)
            tot_s = safe_int(agg['Total Sales'].sum())
            tot_k = safe_int(agg['Total Stock'].sum())
            under = agg[agg['coverage'] < 0.5].sort_values('coverage').head(5)
            over  = agg[(agg['coverage'] > 10) & (agg['Total Sales'] > 0)].sort_values('coverage', ascending=False).head(5)
            good  = agg[(agg['coverage'] >= 0.5) & (agg['coverage'] <= 3)].sort_values('Total Sales', ascending=False).head(5)
            stock_alignment = {
                'total_sales': tot_s, 'total_stock': tot_k,
                'overall_coverage': round(tot_k / tot_s, 2) if tot_s > 0 else 0,
                'understocked_count': int(len(agg[agg['coverage'] < 0.5])),
                'overstocked_count':  int(len(agg[(agg['coverage'] > 10) & (agg['Total Sales'] > 0)])),
                'well_aligned_count': int(len(good)),
                'understocked': under[grp + ['Total Sales', 'Total Stock', 'coverage']].to_dict(orient='records'),
                'overstocked':  over[grp + ['Total Sales', 'Total Stock', 'coverage']].to_dict(orient='records'),
            }

    # ── 4. SALES / DISPATCH / STITCHING ALIGNMENT ─────────────────────
    dispatch_alignment = {}
    if 'Total Sales' in master.columns and 'Total Dispatch' in master.columns:
        grp  = [k for k in ['Category', 'Bag Type'] if k in master.columns]
        need = ['Total Sales', 'Total Dispatch'] + (['Bags stitched'] if 'Bags stitched' in master.columns else [])
        if grp:
            agg = master.groupby(grp, as_index=False)[need].sum()
            tot_s  = safe_int(agg['Total Sales'].sum())
            tot_d  = safe_int(agg['Total Dispatch'].sum())
            tot_st = safe_int(agg['Bags stitched'].sum()) if 'Bags stitched' in agg.columns else 0
            agg['dispatch_lag'] = (agg['Total Sales'] - agg['Total Dispatch']).clip(lower=0)
            lagging = agg[agg['dispatch_lag'] > 0].sort_values('dispatch_lag', ascending=False).head(5)
            dispatch_alignment = {
                'total_sales': tot_s, 'total_dispatch': tot_d, 'total_stitched': tot_st,
                'dispatch_gap':   max(0, tot_s - tot_d),
                'stitch_gap':     max(0, tot_s - tot_st),
                'dispatch_ratio': round(tot_d  / tot_s * 100, 1) if tot_s > 0 else 0,
                'stitch_ratio':   round(tot_st / tot_s * 100, 1) if tot_s > 0 else 0,
                'lagging': lagging[grp + ['Total Sales', 'Total Dispatch', 'dispatch_lag']].to_dict(orient='records'),
            }

    # ── 5. CASH COWS ──────────────────────────────────────────────────
    cash_cows = []
    if not revenue_df.empty and 'Total Revenue' in revenue_df.columns and 'Total Sales' in master.columns:
        grp = [k for k in ['Category', 'Bag Type'] if k in master.columns and k in revenue_df.columns]
        if grp:
            s_agg  = master.groupby(grp, as_index=False)['Total Sales'].sum()
            r_agg  = revenue_df.groupby(grp, as_index=False)['Total Revenue'].sum()
            merged = s_agg.merge(r_agg, on=grp, how='inner')
            merged = merged[merged['Total Sales'] > 0].copy()
            merged['revenue_per_unit'] = (merged['Total Revenue'] / merged['Total Sales']).round(2)
            merged['score']            = (merged['revenue_per_unit'] * merged['Total Sales']).round(0)
            cash_cows = merged.sort_values('score', ascending=False).head(5).to_dict(orient='records')

    # ── 6. WEEKLY ALIGNMENT ───────────────────────────────────────────
    weekly = {}
    wh_cols = ['Bags stitched', 'Bags finished', 'Bags issued for stitching', 'WIP to finishing', 'Stitching WIP']
    avail   = [c for c in wh_cols if c in master.columns]
    if avail:
        w   = {c: safe_int(master[c].sum()) for c in avail}
        s   = safe_int(master['Total Sales'].sum())   if 'Total Sales'   in master.columns else 0
        d   = safe_int(master['Total Dispatch'].sum()) if 'Total Dispatch' in master.columns else 0
        st  = w.get('Bags stitched', 0)
        fin = w.get('Bags finished', 0)
        iss = w.get('Bags issued for stitching', 0)
        pr  = round(st  / s * 100, 1) if s > 0 else 0
        dr  = round(d   / s * 100, 1) if s > 0 else 0
        cr  = round(fin / iss * 100, 1) if iss > 0 else 0
        wk_status = 'good' if pr >= 80 and dr >= 80 else ('moderate' if pr >= 50 or dr >= 50 else 'low')
        weekly = {
            'total_sales': s, 'total_dispatch': d,
            'bags_stitched': st, 'bags_finished': fin, 'bags_issued': iss,
            'finishing_wip': w.get('WIP to finishing', 0), 'stitching_wip': w.get('Stitching WIP', 0),
            'completion_rate': cr, 'prod_ratio': pr, 'dispatch_ratio': dr, 'weekly_status': wk_status,
        }

    return {
        'alerts': alerts, 'target': target_insight,
        'stock_alignment': stock_alignment, 'dispatch_alignment': dispatch_alignment,
        'cash_cows': cash_cows, 'weekly': weekly,
    }


def get_pearson_matrix(data_dict):
    """Pearson correlation matrix across all operational metrics, aggregated at Category/Bag Type level."""
    master    = data_dict['master']
    target_df = data_dict.get('target_df', pd.DataFrame())
    grp = ['Category', 'Bag Type']

    num_cols = [
        'Total Sales', 'Total Stock', 'Total Warehouse Stock',
        'Total Dispatch', 'Total Marketing',
        'Bags in cut store', 'Stitching WIP', 'Bags issued for stitching', 'Bags stitched'
    ]
    avail = [c for c in num_cols if c in master.columns]
    agg   = _safe_groupby_agg(master, grp, avail)

    if not target_df.empty:
        tgt_keys = [k for k in grp if k in target_df.columns]
        tgt_num  = [c for c in ['Monthly sales target', 'Actual sales', 'Deficit'] if c in target_df.columns]
        if tgt_keys and tgt_num:
            tgt_agg  = target_df.groupby(tgt_keys, as_index=False)[tgt_num].sum()
            merge_on = [k for k in tgt_keys if k in agg.columns]
            if merge_on:
                agg = agg.merge(tgt_agg, on=merge_on, how='left')

    value_cols = [c for c in agg.columns if c not in grp]
    numeric_df = agg[value_cols].apply(pd.to_numeric, errors='coerce').dropna(how='all')
    numeric_df = numeric_df.loc[:, numeric_df.std() > 0]   # drop zero-variance columns

    corr = numeric_df.corr(method='pearson')
    labels = list(corr.columns)
    matrix = [
        [round(float(v), 3) if not pd.isna(v) else None for v in row]
        for row in corr.values
    ]

    insights = []
    for i in range(len(labels)):
        for j in range(i + 1, len(labels)):
            r = corr.iloc[i, j]
            if pd.isna(r) or abs(r) < 0.5:
                continue
            insights.append({
                'var_a':     labels[i],
                'var_b':     labels[j],
                'r':         round(float(r), 3),
                'r2':        round(float(r ** 2), 3),
                'direction': 'positive' if r > 0 else 'negative',
                'strength':  'very strong' if abs(r) >= 0.9 else ('strong' if abs(r) >= 0.7 else 'moderate'),
            })
    insights.sort(key=lambda x: abs(x['r']), reverse=True)

    n_strong   = sum(1 for i in insights if abs(i['r']) >= 0.7)
    n_moderate = sum(1 for i in insights if 0.5 <= abs(i['r']) < 0.7)

    return {
        'labels':          labels,
        'matrix':          matrix,
        'insights':        insights[:25],
        'n_observations':  int(numeric_df.dropna().shape[0]),
        'n_variables':     len(labels),
        'n_strong':        n_strong,
        'n_moderate':      n_moderate,
    }
