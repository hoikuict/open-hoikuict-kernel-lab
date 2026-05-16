# コントリビューションガイド

open-hoikuict への改善提案、Issue、Pull Request を歓迎します。

## 基本方針

- 実在する園児・保護者・職員の個人情報を Issue、PR、スクリーンショット、ログに含めないでください。
- デモやテストには `demo_data/` の架空データ、または同等の完全なダミーデータを使ってください。
- 保育現場の運用に関わる変更は、画面の便利さだけでなく、誤入力、権限漏れ、引き継ぎ、監査可能性を確認してください。

## 開発手順

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pytest
uvicorn main:app --reload
```

## Pull Request の目安

- 変更の目的と利用者像を説明する
- 追加・変更した画面やフローをスクリーンショットまたは文章で説明する
- 個人情報・権限・監査ログへの影響を書く
- 可能な範囲でテストを追加する

## コミット・ブランチ

- ブランチ例: `feature/daily-contact-improvement`, `fix/attendance-alert`
- コミットメッセージは日本語または英語で、変更内容が分かる形にしてください。
