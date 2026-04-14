# オペレーションカード: post-incident audit

出典: [personas.md](../../../personas.md) §4.2, §7.5, §8.4, §10

関連参照:
- [../../reference/schema.md](../../reference/schema.md)
- [../../reference/state-machine.md](../../reference/state-machine.md)
- [../../reference/failure-injection.md](../../reference/failure-injection.md) §3.2, §3.3

## このカードを開く場面

- 後日、`human_review_required` や `needs_fix` の理由を説明したいとき
- projection rebuild や再送の正当性を確認したいとき
- mirror 差分や監査コメントの因果を追いたいとき

## 目的

P3 が属人的メモに頼らず、state 遷移、介入理由、rebuild / 再送の履歴を説明できる状態まで再構成する。

## まず確認すること

1. いま見ているものが truth か mirror か。
2. task state 遷移、projection、tool log を混同していないか。
3. 知りたいのが「なぜその state になったか」か、「何が外部へ反映されたか」か。

## この場面で優先すること

- 因果の分離
- truth と mirror の切り分け
- rebuild / 再送を例外ではなく履歴として説明すること

## 追い方

1. `tasks.state`, `state_rev`, `plan_rev` から現在地を確認する。
2. `event_inbox` と internal event で遷移の契機を確認する。
3. `tool_calls` で実行結果と failure を確認する。
4. `projection_outbox` / `projection_cursors` で何が外部へ送られたかを確認する。
5. mirror 側表示は最後に照合し、真実源として扱わない。

## rebuild / 再送を監査するとき

確認項目:
- `idempotency_key` が決定的に再生成されているか
- rebuild が欠損補完なのか、`--force` を伴う再生成なのか
- comment / alert の remote dedup が必要だったか
- `system_events` に永続失敗や復旧の痕跡が残っているか

## やってはいけないこと

- Forgejo mirror だけを見て結論を出すこと
- 再送や rebuild を「例外なので無視」とすること
- task truth と運用メタデータを 1 列の時系列として混ぜること

## このカードの成功条件

- `なぜその state になったか` を説明できる。
- `何がいつ外部へ反映されたか` を説明できる。
- rebuild / 再送の有無と正当性を説明できる。
- 次回も同じ手順で監査を再実行できる。

検証:
- [../../reference/failure-injection.md](../../reference/failure-injection.md) §3.2 Router Transaction
- [../../reference/failure-injection.md](../../reference/failure-injection.md) §3.3 Projection

## このカードの失敗条件

- truth と mirror を混同する。
- rebuild や再送の事実を追えない。
- 介入理由が人間の記憶にしか残っていない。
