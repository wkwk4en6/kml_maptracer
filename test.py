import json
from pmtiles.reader import Reader, MmapSource

# japan.pmtiles のパスを指定
file_path = "world-pmtiles/japan.pmtiles"

with open(file_path, "rb") as f:
    # ソースを読み込んでメタデータを取得
    source = MmapSource(f)
    reader = Reader(source)
    header = reader.header()
    metadata = reader.metadata()
    
    print("=== ヘッダー情報 ===")
    print(header)
    print("\n=== メタデータ（レイヤー・JSON） ===")
    print(json.dumps(metadata, indent=2, ensure_ascii=False))