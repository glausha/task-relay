# オペレーションカード: restore drill

出典: [personas.md](../../../personas.md) §4.3, §7.6, §8.6, §10

関連参照:
- [../../reference/disaster-recovery.md](../../reference/disaster-recovery.md)
- [../../reference/runbook.md](../../reference/runbook.md) §6 restore drill / 保守
- [../../reference/failure-injection.md](../../reference/failure-injection.md) §3.6

## このカードを開く場面

- 定期的な restore drill を行うとき
- backup / replay / rebuild の手順妥当性を確認するとき

## 目的

P4 が runbook をそのまま使い、再現可能な保守作業として restore drill を完走する。

## まず確認すること

1. 今回の作業は task 実行系と混ざっていないか。
2. backup / restore / replay / rebuild の境界を明確に説明できるか。
3. 成功判定が手順内に明示されているか。

## この場面で優先すること

- 手順の再現性
- 真実源を壊さないこと
- 作業後の安全な運用継続確認

## やること

1. runbook の前提条件を確認する。
2. restore drill を手順どおり実施する。
3. 成功判定を記録する。
4. 通常運用へ戻してよいかを確認する。

## やってはいけないこと

- 実運用の task 実行系と保守系の境界を曖昧にすること
- 前回の記憶だけで手順を飛ばすこと
- 成功判定なしに drill を完了扱いにすること

## このカードの成功条件

- runbook どおりに反復実行できる。
- 手順後に安全に運用継続できると確認できる。
- 次回の自分が同じ手順を迷わず再実行できる。

検証:
- [../../reference/failure-injection.md](../../reference/failure-injection.md) §3.6 Disaster Recovery

## このカードの失敗条件

- restore、replay、rebuild の責務が混ざる。
- 手順が属人的で前回メモ前提になる。
- 保守作業が task 実行系へ副作用を与える。
