# 開発者向けセットアップ

## 必要なもの

- Python 3.11 以上
- Git
- ローカル開発用のブラウザ

## セットアップ

```bash
git clone https://github.com/hoikuict/open-hoikuict.git
cd open-hoikuict
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload
```

Windows PowerShell の場合:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn main:app --reload
```

起動後、ブラウザで <http://127.0.0.1:8000/> を開きます。

## テスト

```bash
pytest
```

## データベース

デフォルトではローカルの SQLite ファイル `hoikuict.db` を使います。開発中にデータを作り直したい場合は、事前にバックアップしたうえで削除してください。

```bash
cp hoikuict.db hoikuict.db.bak
rm hoikuict.db
uvicorn main:app --reload
```

## 100人規模デモデータ

```bash
python scripts/seed_demo_100.py --wipe-all
uvicorn main:app --reload
```

`--wipe-all` は対応テーブルの既存データを削除します。本番DBや実在データが入ったDBでは絶対に使わないでください。

## 開発時の注意

- 実在する個人情報をコミットしない
- スクリーンショットに実在情報を含めない
- 権限、監査ログ、エラー時の戻りやすさを確認する
- 保護者画面で他家庭の情報が見えないことを常に確認する
