# Offline Local Map KML Tracer

完全オフライン環境で動作する地図サーバで、KMLファイルの位置情報（GPSログ・タイムスタンプ）をマップ上にプロットし、時系列で詳細に解析・精査するためのローカルWebアプリケーションです。

---

## 機能 (Features)

- 🔒 **完全オフライン動作**: インターネット接続がない環境やクローズドネットワーク上でも、ローカル内の各種ライブラリ（MapLibre GL JS / PMTiles / FlatGeobuf 等）およびローカル地図データを用いてスムーズに動作します。
- 📍 **KML対応・色分け表示**: 複数のKMLファイルを同時に読み込み、各KML（軌跡・地点データ）ごとに異なるカラーで色分け表示・個別管理が可能です。
- ⏳ **時系列フィルタリング & チャート連携**: タイムスライダーや Chart.js によるグラフ描画と連携し、特定の日時・時間帯に絞り込んで地点や移動軌跡を動的に追跡・確認できます。

---

## 動作環境 (Requirements)

- **Python**: 3.12 以上

---

## セットアップ手順 (Setup)

### 1. リポジトリのクローン
```bash
git clone https://github.com/wkwk4en6/kml_maptracer.git

cd kml_maptracer
```

### 2. 仮想環境の作成とライブラリのインストール
本ツールはフォレンジック調査での利用を想定しているため、環境の独立性・再現性およびライブラリ依存関係の管理に優れた Miniconda / Anaconda の使用を推奨しています。

Conda を使用する場合（推奨）:
```bash
# 仮想環境の作成
conda create -n kml_maptracer python=3.12 -y
conda activate kml_maptracer

# 依存ライブラリのインストール
pip install -r requirements.txt
```
pyenv を使用する場合：
```Bash
python -m venv .venv
source .venv/bin/activate  # Windowsの場合は `.venv\Scripts\activate`
pip install -r requirements.txt
```

### 3. ディレクトリの作成と PMTiles データの配置（事前準備）
本サーバの動作には PMTiles データが必要です。はじめに world-pmtiles/ フォルダを作成し、以下手順で取得したデータを配置してください。

#### world-pmtilesフォルダの作成

```Bash
mkdir world-pmtiles
```
#### 広域データの準備

pmtiles CLI ツールを使用し、[Protomaps の公式リモートデータ](https://maps.protomaps.com/builds/)からズームレベル 0〜7 までの広域抽出を行います。ファイルサイズは200MB程度です。   
Windws環境の場合は、コンパイル済みのバイナリを[protomaps/go-pmtiles](https://github.com/protomaps/go-pmtiles/releases)からDLしてください。
Mac環境の場合は、下記コマンドでインストールしてください
```Bash
brew install pmtiles
```
pmtiles ツールをインストールしたら、公式サイトからpmtilesを抽出します。

```Bash
pmtiles extract https://build.protomaps.com/2026xxxx.pmtiles world-pmtiles/planet_z0-z7.pmtiles --maxzoom=7
```
※ _URL の日付部分(2026xxxx)は必要に応じて最新のビルドデータに変更してください。_

#### 詳細マップデータのダウンロード (BBBike)

- BBBike extracts([https://data.bbbike.org/osm/region/](https://data.bbbike.org/osm/region/)) にアクセスします。

- 対象エリアを選択し、Format（フォーマット）で PM Vector tiles Shortbread を選択したら、データをダウンロードして展開します。

- 展開した .pmtiles ファイルを world-pmtiles/ フォルダ内に配置します。

## 実行方法 (Usage)
サーバを起動します。

```Bash
python kml_maptracer.py
```

起動後、ブラウザで以下のURLにアクセスしてください：  
http://localhost:8989

## リポジトリの構成 (Directory Structure)
```Plaintext
.
├── assets/
│   ├── chart/                  # Chart.js ライブラリ類
│   ├── flatgeobuf/             # FlatGeobuf フォーマット用ライブラリ
│   ├── fonts/                  # オフライン用フォントデータ (.pbf)
│   ├── maplibre/               # MapLibre GL JS (地図描画ライブラリ)
│   └── pmtiles/                # PMTiles 解析用ライブラリ
├── world-pmtiles/              # PMTiles データ格納用
│   ├── planet_z0-z7.pmtiles
│   └── *.pmtiles
├── index.html                  # メインUI画面
├── kml_maptracer.py            # ローカルWebサーバー用 Pythonスクリプト
├── requirements.txt            # Python 依存ライブラリ一覧
└── README.md
```
### ライセンス (License)
MIT License