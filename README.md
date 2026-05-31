# open-hoikuict

open-hoikuict は、保育園の日常業務を現場で確かめながら育てていくための、オープンソースの保育ICTプロジェクトです。
園児名簿、家庭・保護者管理、登降園、保護者からの日次連絡、出欠確認、お知らせ、カレンダー、職員ルーム、アンケート、健康情報の管理を段階的に整備しています。

> **現在の位置づけ**  
> このリポジトリは公開準備・デモ・検証段階です。実在する園児・保護者・職員の個人情報、健康情報、連絡先を投入しないでください。
> 本番運用に進む場合は、認証、権限、監査ログ、バックアップ、個人情報保護、サポート体制を各施設・法人の責任で確認してください。

## デモ

- 公式サイト: <https://open.hoikuict.net>
- デモ: <https://demo.hoikuict.net/children/>
- 保育計画作成デモ: <https://plan-writer.hoikuict.net/documents/>

## 主な機能

- 園児・家庭・保護者アカウント管理
- 保護者との日次連絡、欠席連絡、体調連絡
- 登園・降園時刻、迎え予定者、出欠確認
- 出欠・連絡内容の不整合アラート
- お知らせ配信、既読確認
- 園児健康情報、アレルギー、健診記録
- 職員カレンダー、施設共有カレンダー
- 職員ルーム、議事録、アンケート
- CSV/Excel による一部マスタデータの入出力

## ローカルで試す

```bash
git clone https://github.com/hoikuict/open-hoikuict.git
cd open-hoikuict
python -m venv .venv
source .venv/bin/activate  # Windows PowerShell: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn main:app --reload
```

起動後、ブラウザで <http://127.0.0.1:8000/> を開きます。

## 100人規模デモデータを投入する

このリポジトリには、定員100人規模の認可保育園を想定したデモデータを同梱しています。
全データは架空で、メールアドレスは `demo.open-hoikuict.example` ドメインを使っています。

```bash
# 既存DBを消してよいローカル/デモ環境でのみ実行
python -m scripts.seed_demo_100 --wipe-all

# アプリを起動
uvicorn main:app --reload
```

含まれる主なデータは次のとおりです。

- 6クラス、園児100人、家庭84世帯、保護者アカウント156件
- 2026-04-13〜2026-05-15 の登降園・日次連絡データ
- 延長保育料金ルールと日別自動計算済みデータ
- 欠席連絡、体温、睡眠、朝食、排便、服薬、保護者メモ
- 出欠確認と、確認用に用意した不整合アラート
- お知らせ、既読、職員メッセージ、カレンダー予定
- 健康情報、アレルギー、健診記録、園児情報変更申請、アンケート

詳細は [`demo_data/README.md`](demo_data/README.md) と [`docs/demo-data.md`](docs/demo-data.md) を参照してください。

## ドキュメント

- [導入ロードマップ](docs/roadmap.md)
- [開発者向けセットアップ](docs/development.md)
- [運用責任と本番導入前チェック](docs/operations.md)
- [セキュリティ最低ライン](docs/security.md)
- [個人情報・ダミーデータ方針](docs/privacy.md)
- [デモデータ仕様](docs/demo-data.md)
- [職員有給管理機能仕様](docs/paid-leave-management-spec.md)
- [FAQ](docs/faq.md)

## ライセンス

このリポジトリでは MIT License を置いています。正式採用する場合は、著作権者名を確認し、必要に応じて専門家へ相談してください。

## サポートと問い合わせ

- 不具合・改善提案: GitHub Issues
- セキュリティ連絡: `openhoikuict@gmail.com`
- 公式サイト: <https://open.hoikuict.net>

このプロジェクトは無保証で提供されます。自治体提出、監査、補助金、個人情報保護、医療的ケアなどの判断は、各施設・法人・自治体の規程に従ってください。
