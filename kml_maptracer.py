import os
import re
import json
import email.message
import shutil
import xml.etree.ElementTree as ET
from email.header import decode_header
from http.server import SimpleHTTPRequestHandler, HTTPServer
import pandas as pd
import geopandas as gpd
import urllib.parse

# geopy (オンライン時用のライブラリは残しつつ、オフラインは reverse_geocoder を利用)
from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut, GeocoderServiceError

import reverse_geocoder as rg
import pycountry

# Nominatim インスタンスの初期化
geolocator = Nominatim(user_agent="range_server_app")

# メモリ上に読み込んだFGB/KML データのキャッシュ保持用辞書
DATA_CACHE = {}

def safe_decode_header(header_val):
    """MIMEエンコードおよびRFC2231/UTF-8生のファイル名を安全にデコードする"""
    if not header_val:
        return "uploaded_track.kml"
    
    # RFC 2231 形式 (filename*=utf-8''...) の処理
    if "''" in header_val:
        try:
            return urllib.parse.unquote(header_val.split("''")[-1])
        except Exception:
            pass

    try:
        decoded_segments = decode_header(header_val)
        result_bytes = b""
        for text, encoding in decoded_segments:
            if isinstance(text, bytes):
                if encoding:
                    try:
                        result_bytes += text.decode(encoding).encode('utf-8')
                    except Exception:
                        # cp932/shift_jis などの フォールバック
                        try:
                            result_bytes += text.decode('cp932').encode('utf-8')
                        except Exception:
                            result_bytes += text
                else:
                    try:
                        result_bytes += text.decode('utf-8').encode('utf-8')
                    except Exception:
                        try:
                            result_bytes += text.decode('cp932').encode('utf-8')
                        except Exception:
                            result_bytes += text
            elif isinstance(text, str):
                result_bytes += text.encode('utf-8')
        
        decoded_str = result_bytes.decode('utf-8', errors='replace')
        return decoded_str if decoded_str else "uploaded_track.kml"
    except Exception:
        return str(header_val)

def generate_config_json():
    """world-pmtiles フォルダ内の .pmtiles ファイルを検索し countries.json を更新"""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    world_dir = os.path.join(base_dir, 'world-pmtiles')
    output_path = os.path.join(base_dir, 'countries.json')

    countries = []
    if os.path.exists(world_dir):
        for file in os.listdir(world_dir):
            if file.endswith('.pmtiles') and file != 'planet_z0-z7.pmtiles' and not file.startswith('.'):
                country_id = os.path.splitext(file)[0]
                countries.append(country_id)

    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(countries, f, ensure_ascii=False, indent=2)
    print(f"[Info] Updated countries.json: {countries}")


def get_country_name(code):
    """国コード (JP) から国名 (Japan) を取得"""
    if not code:
        return ''
    try:
        country = pycountry.countries.get(alpha_2=code.upper())
        return country.name if country else code
    except Exception:
        return code


def apply_offline_geocoding(gdf):
    """
    GeoDataFrameの各ポイントに対して reverse_geocoder を適用し、
    prefecture, city_town, country_code, address カラムを一括追加する関数。
    address の先頭には国コード（または国識別子）を自動付与。
    """
    if gdf is None or gdf.empty:
        return gdf

    coords = [(geom.y, geom.x) for geom in gdf.geometry if hasattr(geom, 'x') and hasattr(geom, 'y')]

    if not coords:
        gdf['country_code'] = ''
        gdf['prefecture'] = ''
        gdf['city_town'] = ''
        gdf['address'] = ''
        return gdf

    rg_results = rg.search(coords)

    country_codes = []
    prefectures = []
    cities = []
    addresses = []

    for res in rg_results:
        cc = res.get('cc', '').upper()
        c_name = get_country_name(cc)
        province = res.get('admin1', '')
        city = res.get('name', '')
        
        components = [c_name, province, city]
        formatted = " ".join([c for c in components if c])

        country_codes.append(cc)
        prefectures.append(province)
        cities.append(city)
        addresses.append(formatted)

    gdf['country_code'] = country_codes
    gdf['prefecture'] = prefectures
    gdf['city_town'] = cities
    gdf['address'] = addresses

    return gdf


def post_process_kml(kml_path):
    """
    生成されたKMLファイルに対し、
    1. <TimeStamp><when>...</when></TimeStamp> の挿入
    2. 文字化け文字（）の除去・UTF-8の正規化
    を行う後処理関数
    """
    ET.register_namespace('', "http://www.opengis.net/kml/2.2")
    ns = {'kml': 'http://www.opengis.net/kml/2.2'}

    tree = ET.parse(kml_path)
    root = tree.getroot()

    for placemark in root.findall('.//kml:Placemark', ns):
        # TimeStampが未存在の場合、ExtendedData内のtimestamp/dtから判定して作成
        if placemark.find('kml:TimeStamp', ns) is None:
            time_val = None
            
            # SimpleData 内の timestamp または dt を検索
            for sd in placemark.findall('.//kml:SimpleData', ns):
                name_attr = sd.attrib.get('name')
                if name_attr in ['timestamp', 'dt'] and sd.text:
                    time_val = sd.text.strip()
                    break

            if time_val:
                # ISO 8601 形式 (YYYY-MM-DDTHH:MM:SSZ) へフォーマットを調整
                formatted_time = time_val.replace(' ', 'T')
                if not formatted_time.endswith('Z') and '+00:00' in formatted_time:
                    formatted_time = formatted_time.replace('+00:00', 'Z')
                elif not formatted_time.endswith('Z') and '+' not in formatted_time and '-' not in formatted_time[10:]:
                    formatted_time += 'Z'

                # <TimeStamp><when>...</when></TimeStamp> を生成して Placemark に追加
                timestamp_elem = ET.Element('{http://www.opengis.net/kml/2.2}TimeStamp')
                when_elem = ET.SubElement(timestamp_elem, '{http://www.opengis.net/kml/2.2}when')
                when_elem.text = formatted_time

                # Placemark 直下に挿入
                placemark.insert(0, timestamp_elem)

        # 文字化け記号 ( / U+FFFD) を含むSimpleDataテキストのクリーンアップ
        for sd in placemark.findall('.//kml:SimpleData', ns):
            if sd.text and '\ufffd' in sd.text:
                sd.text = sd.text.replace('\ufffd', '').strip()
                if not sd.text:
                    sd.text = "unknown_file.kml"

    tree.write(kml_path, encoding='utf-8', xml_declaration=True)


class ExtendedRequestHandler(SimpleHTTPRequestHandler):

    def do_POST(self):
        # ----------------------------------------------------
        # 単発の座標逆ジオコーディング (/reverse_geocode)
        # ----------------------------------------------------
        if self.path == '/reverse_geocode':
            try:
                content_length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(content_length)
                req_data = json.loads(body.decode('utf-8')) if body else {}

                lat = float(req_data.get('lat'))
                lng = float(req_data.get('lng'))

                results = rg.search((lat, lng))

                if results and len(results) > 0:
                    res = results[0]

                    province = res.get('admin1', '')
                    city = res.get('name', '')
                    country_code = res.get('cc', '').upper()

                    components = [country_code, province, city]
                    formatted_address = " ".join([c for c in components if c])

                    if not formatted_address:
                        formatted_address = f"{lat}, {lng}"

                    response_data = {
                        'status': 'success',
                        'lat': lat,
                        'lng': lng,
                        'prefecture': province,
                        'city_town': city,
                        'country_code': country_code,
                        'formatted_address': formatted_address
                    }
                else:
                    response_data = {
                        'status': 'not_found',
                        'formatted_address': "住所判定不可"
                    }

                response_bytes = json.dumps(response_data, ensure_ascii=False).encode('utf-8')
                self.send_response(200)
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(response_bytes)))
                self.end_headers()
                self.wfile.write(response_bytes)

            except Exception as e:
                print(f"[ERROR /reverse_geocode] 逆ジオコーディング処理エラー: {e}")
                err_bytes = json.dumps({'error': str(e)}).encode('utf-8')
                self.send_response(500)
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(err_bytes)))
                self.end_headers()
                self.wfile.write(err_bytes)

        # 1. 時間範囲に応じたデータフィルタリング＆出力エンドポイント (/filter_points)
        elif self.path == '/filter_points':
            try:
                content_length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(content_length)
                req_data = json.loads(body.decode('utf-8')) if body else {}

                start_time = req_data.get('start_time')
                end_time = req_data.get('end_time')
                limit = req_data.get('limit')

                filtered_features = []
                total_matched = 0

                for file_id, file_info in DATA_CACHE.items():
                    gdf = file_info['gdf']
                    if gdf.empty:
                        continue

                    df_filtered = gdf
                    if 'timestamp' in gdf.columns:
                        if start_time:
                            df_filtered = df_filtered[df_filtered['dt'] >= pd.to_datetime(start_time)]
                        if end_time:
                            df_filtered = df_filtered[df_filtered['dt'] <= pd.to_datetime(end_time)]

                    total_matched += len(df_filtered)

                    if limit is not None and len(df_filtered) > limit:
                        df_filtered = df_filtered.iloc[::(len(df_filtered) // limit + 1)]

                    for _, row in df_filtered.iterrows():
                        coords = [row.geometry.x, row.geometry.y] if hasattr(row.geometry, 'x') else None
                        if not coords:
                            continue

                        filtered_features.append({
                            'id': str(row.get('id', '')),
                            'seq': int(row.get('seq', 0)),
                            'file_id': file_id,
                            'file_name': file_info['name'],
                            'color': file_info['color'],
                            'timestamp': str(row.get('timestamp', '')),
                            'lng': coords[0],
                            'lat': coords[1],
                            'address': str(row.get('address', '')),
                            'country_code': str(row.get('country_code', '')),
                            'prefecture': str(row.get('prefecture', '')),
                            'city_town': str(row.get('city_town', ''))
                        })

                response_data = {
                    'total_matched': total_matched,
                    'returned_count': len(filtered_features),
                    'points': filtered_features
                }

                print(f"[LOG /filter_points] 抽出完了: 条件マッチ={total_matched}件, 返却={len(filtered_features)}件")

                response_bytes = json.dumps(response_data, ensure_ascii=False).encode('utf-8')
                self.send_response(200)
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(response_bytes)))
                self.end_headers()
                self.wfile.write(response_bytes)

            except Exception as e:
                print(f"[ERROR /filter_points] フィルタ処理失敗: {e}")
                err_bytes = json.dumps({'error': str(e)}).encode('utf-8')
                self.send_response(400)
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(err_bytes)))
                self.end_headers()
                self.wfile.write(err_bytes)

        # 2. 時間別件数の集計処理エンドポイント (/aggregate)
        elif self.path == '/aggregate':
            try:
                content_length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(content_length)
                req_data = json.loads(body.decode('utf-8')) if body else {}

                interval = req_data.get('interval', '1h').replace('H', 'h')
                updated_colors = req_data.get('colors', {})

                all_dfs = []
                for file_id, file_info in DATA_CACHE.items():
                    gdf = file_info['gdf'].copy()
                    if not gdf.empty and 'dt' in gdf.columns:
                        color_val = updated_colors.get(file_id)
                        if isinstance(color_val, dict):
                            current_color = color_val.get('color', file_info.get('color', '#0078ff'))
                        elif isinstance(color_val, str):
                            current_color = color_val
                        else:
                            current_color = file_info.get('color', '#0078ff')

                        file_info['color'] = current_color
                        gdf['color'] = current_color
                        gdf['file_id'] = file_id
                        all_dfs.append(gdf)

                if not all_dfs:
                    response_data = {'labels': [], 'datasets': []}
                else:
                    df = pd.concat(all_dfs, ignore_index=True)
                    unit = interval.lower()

                    if unit in ['all', '全範囲', '全ての範囲']:
                        df['bucket'] = '全期間'
                        fmt = None
                    elif unit in ['1y', 'y', 'year', 'years']:
                        df['bucket'] = df['dt'].dt.to_period('Y').dt.to_timestamp()
                        fmt = '%Y年'
                    elif unit in ['3m', '3month', '3months']:
                        df['bucket'] = df['dt'].dt.to_period('Q')
                        fmt = None
                    elif unit in ['1m', 'm', 'month', 'months']:
                        df['bucket'] = df['dt'].dt.to_period('M').dt.to_timestamp()
                        fmt = '%Y-%m'
                    elif unit in ['1w', 'w', 'week', 'weeks']:
                        df['bucket'] = df['dt'].dt.to_period('W').dt.to_timestamp()
                        fmt = '%Y-%m-%d (週)'
                    elif unit in ['1d', 'd', 'day', 'days']:
                        df['bucket'] = df['dt'].dt.floor('D')
                        fmt = '%Y-%m-%d'
                    elif unit in ['30m', '30min']:
                        df['bucket'] = df['dt'].dt.floor('30min')
                        fmt = '%Y-%m-%d %H:%M'
                    elif unit in ['15m', '15min']:
                        df['bucket'] = df['dt'].dt.floor('15min')
                        fmt = '%Y-%m-%d %H:%M'
                    elif unit in ['5m', '5min']:
                        df['bucket'] = df['dt'].dt.floor('5min')
                        fmt = '%Y-%m-%d %H:%M'
                    else:
                        df['bucket'] = df['dt'].dt.floor(unit if unit else '1h')
                        fmt = '%Y-%m-%d %H:%M'

                    pivot = pd.crosstab(df['bucket'], df['file_id'])

                    if unit in ['3m', '3month', '3months']:
                        labels = [f"{p.year}-Q{p.quarter}" for p in pivot.index]
                    elif fmt and hasattr(pivot.index, 'strftime'):
                        labels = pivot.index.strftime(fmt).tolist()
                    else:
                        labels = [str(x) for x in pivot.index.tolist()]

                    datasets = []
                    for fid in pivot.columns:
                        info = DATA_CACHE.get(fid, {})
                        datasets.append({
                            'file_id': fid,
                            'file_name': info.get('name', fid),
                            'color': info.get('color', '#0078ff'),
                            'color_name': info.get('color', '#0078ff'),
                            'counts': pivot[fid].tolist()
                        })

                    response_data = {
                        'labels': labels,
                        'datasets': datasets
                    }

                response_bytes = json.dumps(response_data, ensure_ascii=False).encode('utf-8')
                self.send_response(200)
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(response_bytes)))
                self.end_headers()
                self.wfile.write(response_bytes)

            except Exception as e:
                print(f"[ERROR /aggregate] 集計処理失敗: {e}")
                err_bytes = json.dumps({'error': str(e)}).encode('utf-8')
                self.send_response(400)
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(err_bytes)))
                self.end_headers()
                self.wfile.write(err_bytes)

        # 3. KML/FGB 変換・アップロード・バックエンド保持・FGBバイナリ返却処理 (/upload)
        elif self.path == '/upload':
            temp_kml_path = None
            fgb_path = None

            try:
                content_type = self.headers.get('Content-Type')
                content_length = int(self.headers.get('Content-Length', 0))

                if not content_type or 'multipart/form-data' not in content_type:
                    self.send_error(400, "Bad Request: Expected multipart/form-data")
                    return

                body = self.rfile.read(content_length)

                msg_bytes = f"Content-Type: {content_type}\r\n\r\n".encode('utf-8', errors='surrogateescape') + body
                msg = email.message_from_bytes(msg_bytes)

                file_data = None
                original_filename = "uploaded_track.kml"
                color = "青"

                for part in msg.walk():
                    fn = part.get_filename()
                    disp = part.get('Content-Disposition', '')

                    if fn or 'filename=' in disp:
                        file_data = part.get_payload(decode=True)
                        
                        if fn:
                            original_filename = safe_decode_header(fn)
                        elif 'filename=' in disp:
                            raw_fn = re.search(r'filename\*?=(?:UTF-8\'\')?["\']?([^"\';]+)["\']?', disp, re.IGNORECASE)
                            if raw_fn:
                                raw_val = raw_fn.group(1)
                                original_filename = safe_decode_header(urllib.parse.unquote(raw_val))

                    elif 'name="color"' in disp:
                        color = part.get_payload(decode=True).decode('utf-8', errors='ignore')

                if not file_data:
                    self.send_error(400, "Bad Request: File not found in POST request")
                    return

                base_dir = os.path.dirname(os.path.abspath(__file__))
                temp_dir = os.path.join(base_dir, 'temp_uploads')
                os.makedirs(temp_dir, exist_ok=True)

                file_id = f"file_{len(DATA_CACHE) + 1}"
                temp_kml_path = os.path.join(temp_dir, f'{file_id}.kml')
                fgb_path = os.path.join(temp_dir, f'{file_id}.fgb')

                with open(temp_kml_path, 'wb') as f:
                    f.write(file_data)

                from shapely import force_2d
                from shapely.geometry import shape
                import fiona

                fiona.drvsupport.supported_drivers['KML'] = 'rw'
                fiona.drvsupport.supported_drivers['LIBKML'] = 'rw'

                gdf = None

                try:
                    with fiona.Env(SHAPE_ENCODING='utf-8', KML_USE_OPTION='YES'):
                        gdf = gpd.read_file(temp_kml_path, driver='KML', encoding='utf-8')
                except Exception:
                    try:
                        gdf = gpd.read_file(temp_kml_path)
                    except Exception as e2:
                        print(f"[WARNING] GeoPandas 読み込み失敗: {e2}")

                if gdf is None or gdf.empty:
                    try:
                        layers = fiona.listlayers(temp_kml_path)
                        features = []
                        for layer_name in layers:
                            with fiona.open(temp_kml_path, layer=layer_name, encoding='utf-8') as src:
                                for feat in src:
                                    if feat.get('geometry'):
                                        item = dict(feat.get('properties', {}) or {})
                                        item['geometry'] = force_2d(shape(feat['geometry']))
                                        features.append(item)
                        if features:
                            gdf = gpd.GeoDataFrame(features)
                    except Exception as kml_err:
                        print(f"[WARNING] Fiona 読み込み失敗: {kml_err}")

                if gdf is None or gdf.empty:
                    raise ValueError("No valid coordinates or geometries found in KML file.")

                gdf.geometry = gdf.geometry.apply(force_2d)
                gdf.crs = "EPSG:4326"

                # 【追加】ファイルごとに1から始まる連番 (seq) を全行に割り当てる
                gdf['seq'] = list(range(1, len(gdf) + 1))
                # 既存の 'id' カラムが存在しない・空の場合は seq を文字列表現としてセット
                if 'id' not in gdf.columns or gdf['id'].isnull().all():
                    gdf['id'] = gdf['seq'].astype(str)

                print(f"[LOG /upload] オフライン逆ジオコーディングを開始します (全 {len(gdf)} 件)...")
                gdf = apply_offline_geocoding(gdf)

                # 空間インデックスの作成を無効化（kmlファイルの順序のまま）
                gdf.to_file(fgb_path, driver="FlatGeobuf", SPATIAL_INDEX="NO")

                if 'timestamp' in gdf.columns:
                    gdf['dt'] = pd.to_datetime(gdf['timestamp'])

                DATA_CACHE[file_id] = {
                    'gdf': gdf,
                    'name': original_filename,
                    'color': color,
                    'fgb_path': fgb_path
                }

                print(f"[SUCCESS /upload] バックエンドに保持完了 & FGBバイナリを返却: {file_id} ({original_filename}), 件数={len(gdf)}")

                fgb_size = os.path.getsize(fgb_path)
                out_filename = os.path.splitext(original_filename)[0] + ".fgb"

                self.send_response(200)
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Access-Control-Expose-Headers', 'X-File-Id, Content-Disposition')
                self.send_header('Content-Type', 'application/octet-stream')
                self.send_header('Content-Length', str(fgb_size))
                self.send_header('X-File-Id', file_id)
                self.send_header('Content-Disposition', f'attachment; filename="{urllib.parse.quote(out_filename)}"')
                self.end_headers()

                with open(fgb_path, 'rb') as fgb_file:
                    shutil.copyfileobj(fgb_file, self.wfile)

            except Exception as e:
                print(f"[ERROR] 変換・読み込み処理エラー: {e}")
                self.send_error(500, f"Internal Server Error: {e}")

            finally:
                if temp_kml_path and os.path.exists(temp_kml_path):
                    try:
                        os.remove(temp_kml_path)
                    except Exception as cleanup_err:
                        print(f"[WARNING] KML削除失敗: {cleanup_err}")

        # 5. 単位面積毎の密度集計処理 (/heatmap)
        elif self.path == '/heatmap':
            try:
                content_length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(content_length)
                raw_data = json.loads(body.decode('utf-8')) if body else {}

                if isinstance(raw_data, list):
                    req_data = raw_data[0] if len(raw_data) > 0 else {}
                elif isinstance(raw_data, dict):
                    req_data = raw_data
                else:
                    req_data = {}

                start_time = req_data.get('start_time')
                end_time = req_data.get('end_time')
                bounds = req_data.get('bounds', {})
                grid_size_m = float(req_data.get('grid_size_m', 100))

                all_points = []
                for file_id, file_info in DATA_CACHE.items():
                    gdf = file_info['gdf']
                    if gdf.empty:
                        continue

                    df_filtered = gdf
                    if 'timestamp' in gdf.columns:
                        if start_time:
                            df_filtered = df_filtered[df_filtered['dt'] >= pd.to_datetime(start_time)]
                        if end_time:
                            df_filtered = df_filtered[df_filtered['dt'] <= pd.to_datetime(end_time)]

                    for _, row in df_filtered.iterrows():
                        if hasattr(row.geometry, 'x'):
                            all_points.append({'lng': row.geometry.x, 'lat': row.geometry.y})

                if not all_points:
                    geo_json = {"type": "FeatureCollection", "features": []}
                else:
                    gdf_pts = gpd.GeoDataFrame(
                        all_points,
                        geometry=gpd.points_from_xy([p['lng'] for p in all_points], [p['lat'] for p in all_points]),
                        crs="EPSG:4326"
                    )

                    if isinstance(bounds, dict) and bounds:
                        gdf_pts = gdf_pts[
                            (gdf_pts['lng'] >= bounds.get('min_lng', -180)) &
                            (gdf_pts['lng'] <= bounds.get('max_lng', 180)) &
                            (gdf_pts['lat'] >= bounds.get('min_lat', -90)) &
                            (gdf_pts['lat'] <= bounds.get('max_lat', 90))
                        ]

                    if gdf_pts.empty:
                        geo_json = {"type": "FeatureCollection", "features": []}
                    else:
                        gdf_projected = gdf_pts.to_crs(epsg=3857)
                        
                        gdf_projected['grid_x'] = (gdf_projected.geometry.x // grid_size_m) * grid_size_m + (grid_size_m / 2)
                        gdf_projected['grid_y'] = (gdf_projected.geometry.y // grid_size_m) * grid_size_m + (grid_size_m / 2)

                        grouped = gdf_projected.groupby(['grid_x', 'grid_y']).size().reset_index(name='count')

                        cell_area_km2 = (grid_size_m * grid_size_m) / 1_000_000.0
                        grouped['density_per_km2'] = (grouped['count'] / cell_area_km2).round(2)

                        grid_centers = gpd.GeoDataFrame(
                            grouped,
                            geometry=gpd.points_from_xy(grouped['grid_x'], grouped['grid_y']),
                            crs="EPSG:3857"
                        ).to_crs(epsg=4326)

                        features = []
                        for _, row in grid_centers.iterrows():
                            features.append({
                                "type": "Feature",
                                "geometry": {
                                    "type": "Point",
                                    "coordinates": [row.geometry.x, row.geometry.y]
                                },
                                "properties": {
                                    "count": int(row['count']),
                                    "grid_size_m": grid_size_m,
                                    "area_km2": cell_area_km2,
                                    "density_per_km2": float(row['density_per_km2'])
                                }
                            })

                        geo_json = {"type": "FeatureCollection", "features": features}

                response_bytes = json.dumps(geo_json, ensure_ascii=False).encode('utf-8')
                self.send_response(200)
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(response_bytes)))
                self.end_headers()
                self.wfile.write(response_bytes)

            except Exception as e:
                print(f"[ERROR /heatmap] 集計エラー: {e}")
                err_bytes = json.dumps({'error': str(e)}).encode('utf-8')
                self.send_response(400)
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(err_bytes)))
                self.end_headers()
                self.wfile.write(err_bytes)

        # 0. キャッシュ削除処理 (/clear_cache)
        elif self.path == '/clear_cache':
            try:
                DATA_CACHE.clear()
                print("[LOG /clear_cache] DATA_CACHE をクリアしました")

                base_dir = os.path.dirname(os.path.abspath(__file__))
                temp_dir = os.path.join(base_dir, 'temp_uploads')

                if os.path.exists(temp_dir):
                    for filename in os.listdir(temp_dir):
                        file_path = os.path.join(temp_dir, filename)
                        try:
                            if os.path.isfile(file_path) or os.path.islink(file_path):
                                os.unlink(file_path)
                            elif os.path.isdir(file_path):
                                shutil.rmtree(file_path)
                        except Exception as file_err:
                            print(f"[WARNING /clear_cache] ファイル削除失敗 ({file_path}): {file_err}")
                    print("[LOG /clear_cache] temp_uploads フォルダ内をクリアしました")

                response_data = {'status': 'success', 'message': 'Cache and temp files cleared'}
                response_bytes = json.dumps(response_data, ensure_ascii=False).encode('utf-8')
                self.send_response(200)
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(response_bytes)))
                self.end_headers()
                self.wfile.write(response_bytes)
            except Exception as e:
                print(f"[ERROR /clear_cache] クリア処理失敗: {e}")
                self.send_error(500, f"Internal Server Error: {e}")

        # ----------------------------------------------------
        # 6. フロントエンドからのフィルタ済みポイントJSONを受信して CSV/KML へ変換・ダウンロード (/export)
        # ----------------------------------------------------
        elif self.path == '/export' or self.path.startswith('/export'):
            out_path = None
            try:
                content_length = int(self.headers.get('Content-Length', 0))
                if content_length == 0:
                    self.send_error(400, "Bad Request: Empty body")
                    return

                body = self.rfile.read(content_length)
                req_data = json.loads(body.decode('utf-8')) if body else {}

                export_format = req_data.get('format', 'csv').lower()
                req_points = req_data.get('points', [])
                print(f"[DEBUG /export] 受信件数: {len(req_points)}")

                if not req_points:
                    self.send_error(400, "Bad Request: No points provided for export")
                    return

                exported_rows = []
                for pt in req_points:
                    file_id = pt.get('file_id') or pt.get('fileId') or pt.get('track_id')
                    pt_id = pt.get('id')
                    seq = pt.get('seq')
                    timestamp = pt.get('timestamp')

                    matched = pd.DataFrame()

                    if file_id and file_id in DATA_CACHE:
                        gdf = DATA_CACHE[file_id]['gdf']
                        
                        if pt_id is not None and 'id' in gdf.columns:
                            matched = gdf[gdf['id'].astype(str) == str(pt_id)]
                        
                        if matched.empty and seq is not None and 'seq' in gdf.columns:
                            matched = gdf[gdf['seq'].astype(str) == str(seq)]
                            
                        if matched.empty and timestamp is not None and 'timestamp' in gdf.columns:
                            matched = gdf[gdf['timestamp'].astype(str) == str(timestamp)]

                        if matched.empty and seq is not None:
                            try:
                                idx = int(seq) - 1
                                if 0 <= idx < len(gdf):
                                    matched = gdf.iloc[[idx]]
                            except ValueError:
                                pass

                        if not matched.empty:
                            row_gdf = matched.iloc[[0]].copy()
                            row_gdf['source_file_id'] = file_id
                            row_gdf['source_file_name'] = DATA_CACHE[file_id].get('name', '')
                            exported_rows.append(row_gdf)

                    if matched.empty:
                        lng = pt.get('lng') or pt.get('longitude')
                        lat = pt.get('lat') or pt.get('latitude')
                        if lng is not None and lat is not None:
                            from shapely.geometry import Point
                            pt_dict = {k: v for k, v in pt.items() if k not in ['lng', 'lat', 'longitude', 'latitude']}
                            pt_gdf = gpd.GeoDataFrame([pt_dict], geometry=[Point(float(lng), float(lat))], crs="EPSG:4326")
                            exported_rows.append(pt_gdf)

                if not exported_rows:
                    self.send_error(404, "Not Found: No matching points found in cache")
                    return

                export_gdf = pd.concat(exported_rows, ignore_index=True)
                if not isinstance(export_gdf, gpd.GeoDataFrame):
                    export_gdf = gpd.GeoDataFrame(export_gdf, crs="EPSG:4326")

                base_dir = os.path.dirname(os.path.abspath(__file__))
                temp_dir = os.path.join(base_dir, 'temp_uploads')
                os.makedirs(temp_dir, exist_ok=True)

                import uuid
                export_id = str(uuid.uuid4())[:8]

                # --- 1. CSV 出力 ---
                if export_format == 'csv':
                    out_path = os.path.join(temp_dir, f'export_{export_id}.csv')
                    df = export_gdf.copy()

                    if 'geometry' in df.columns:
                        if 'longitude' not in df.columns:
                            df['longitude'] = df.geometry.apply(lambda g: g.x if hasattr(g, 'x') and g else None)
                        if 'latitude' not in df.columns:
                            df['latitude'] = df.geometry.apply(lambda g: g.y if hasattr(g, 'y') and g else None)
                        
                        df['geometry_wkt'] = df.geometry.apply(lambda g: g.wkt if g else '')
                        df = df.drop(columns=['geometry'])

                    cols_to_drop = [c for c in ['dt'] if c in df.columns]
                    if cols_to_drop:
                        df = df.drop(columns=cols_to_drop)

                    df.to_csv(out_path, index=False, encoding='utf-8-sig')
                    content_type = 'text/csv; charset=utf-8'
                    filename = f'exported_points_{export_id}.csv'

                # --- 2. KML 出力 ---
                elif export_format == 'kml':
                    out_path = os.path.join(temp_dir, f'export_{export_id}.kml')
                    import fiona
                    fiona.drvsupport.supported_drivers['KML'] = 'rw'
                    fiona.drvsupport.supported_drivers['LIBKML'] = 'rw'

                    for col in export_gdf.columns:
                        if col != 'geometry' and pd.api.types.is_datetime64_any_dtype(export_gdf[col]):
                            export_gdf[col] = export_gdf[col].astype(str)

                    with fiona.Env(SHAPE_ENCODING='utf-8', KML_USE_OPTION='YES'):
                        export_gdf.to_file(out_path, driver='KML', encoding='utf-8')

                    # 追加: 後処理による <TimeStamp> タグの挿入および文字化けクリーンアップ
                    post_process_kml(out_path)

                    content_type = 'application/vnd.google-earth.kml+xml'
                    filename = f'exported_points_{export_id}.kml'

                else:
                    self.send_error(400, f"Unsupported format: {export_format}")
                    return

                file_size = os.path.getsize(out_path)
                self.send_response(200)
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Access-Control-Expose-Headers', 'Content-Disposition')
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(file_size))
                self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
                self.end_headers()

                with open(out_path, 'rb') as out_f:
                    shutil.copyfileobj(out_f, self.wfile)

                print(f"[SUCCESS /export] エクスポート完了 ({export_format.upper()}, 対象={len(export_gdf)}件)")

            except Exception as e:
                print(f"[ERROR /export] エクスポート処理失敗: {e}")
                err_bytes = json.dumps({'error': str(e)}).encode('utf-8')
                self.send_response(500)
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(err_bytes)))
                self.end_headers()
                self.wfile.write(err_bytes)

            finally:
                if out_path and os.path.exists(out_path):
                    try:
                        os.remove(out_path)
                    except Exception:
                        pass
        else:
            self.send_error(404, "Not Found")

    def do_OPTIONS(self):
        """CORSプリフライト対応"""
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Range')
        self.end_headers()

    def send_head(self):
        """既存のRange Request処理"""
        path = self.translate_path(self.path)

        if not os.path.exists(path):
            self.send_error(404, "File not found")
            return None

        if os.path.isdir(path):
            return super().send_head()

        self.extensions_map.update({
            '.pmtiles': 'application/octet-stream',
            '.fgb': 'application/octet-stream',
            '.kml': 'application/xml',
            '.json': 'application/json'
        })

        range_header = self.headers.get('Range')
        if not range_header or not range_header.startswith('bytes='):
            return super().send_head()

        size = os.path.getsize(path)
        m = re.match(r'bytes=(\d+)-(\d*)', range_header)
        if not m:
            return super().send_head()

        start, end = m.groups()
        start = int(start)
        end = int(end) if end else size - 1

        if start >= size or end >= size or start > end:
            self.send_error(416, 'Requested Range Not Satisfiable')
            return None

        self.send_response(206)
        self.send_header('Content-Type', self.guess_type(path))
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        self.send_header('Content-Length', str(end - start + 1))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()

        f = open(path, 'rb')
        f.seek(start)
        return f

    def end_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        super().end_headers()


if __name__ == '__main__':
    from socketserver import ThreadingTCPServer

    class QuietThreadingTCPServer(ThreadingTCPServer):
        def handle_error(self, request, client_address):
            import sys
            exctype, value = sys.exc_info()[:2]
            if issubclass(exctype, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
                return
            super().handle_error(request, client_address)

    generate_config_json()

    with QuietThreadingTCPServer(('127.0.0.1', 8989), ExtendedRequestHandler) as server:
        print("Serving HTTP on 0.0.0.0 port 8989 (http://localhost:8989/) ...")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nKeyboard interrupt received, exiting.")