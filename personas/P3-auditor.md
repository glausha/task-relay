# P3: 変更履歴を後から検証する監査者としての自分 (Secondary)

> マスター: `../personas.md` §4.2

## 一言サマリ

数日〜数週後に「なぜこの変更が起きたか」を再構成する自分。PR レビュー後、障害後の再発防止、長期運用挙動の調査で登場する。

## 場面 / Device / Surface

- 場所: デスク、時間余裕あり。
- Device: PC。
- Surface: Forgejo、runner-cli、log tail。

## 認知予算

- 時間制約なし。数時間投入できる。
- 非同期、レポート形式と親和。

## 最重要ニーズ

- task ごとの状態遷移が時系列で追える。
- AI 実行・人間承認・投影結果が分離して記録される。
- 真実源と mirror の役割差が明確である。

## 成功

- state 遷移を時系列で漏れなく追える。
- AI 実行 / 人間承認 / 投影結果が分離記録されている。
- projection 再送や rebuild の事実が説明可能である。

## 失敗

- state / projection / tool log が混在していて解読不能。
- mirror を真実と誤認した記録。
- 再送や rebuild の事実がログから消えている。

## 代表 utterance

- 「なんでこの task は `human_review_required` になったんだっけ?」
- 「この comment はいつ誰がつけた?」
- 「先月の `system_degraded` は何が root cause だった?」

## 設計含意

- task truth と operational metadata を混ぜない。
- 監査コメントと current snapshot を分ける。
- 真実源を明示し、mirror を真実源として参照しない。

## 関連シナリオ

- マスター §7.5 事後監査
