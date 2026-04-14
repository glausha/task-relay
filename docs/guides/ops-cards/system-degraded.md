# オペレーションカード: system_degraded

出典: [personas.md](../../../personas.md) §4.1, §7.4, §8.1, §8.2, §10

関連参照:
- [../../reference/runbook.md](../../reference/runbook.md) §1.1 operator scope の正規化
- [../../reference/state-machine.md](../../reference/state-machine.md) §8 operator 向け参照
- [../../reference/failure-injection.md](../../reference/failure-injection.md) §3.4, §3.5

## このカードを開く場面

- 全体障害通知を受けたとき
- breaker open や dispatch stop を示す通知を見たとき

補足:
- `system_degraded` は全体障害である。
- breaker open は全体保護であり、全体影響ではあるが task state ではない。

## 目的

深夜でも 1 分以内に、`今すぐ復旧操作を始めるか`、`安全に待機できるか` を決める。

## まず確認すること

1. これは task 単位ではなく system 単位の停止か。
2. 今止まっているのは dispatch か、既存 in-flight も含むか。
3. 朝まで待ってよい条件が通知や runbook に明示されているか。

## この場面で優先すること

- 局所障害と全体障害を混同しない。
- 無理に個別 task をいじらない。
- 状況を悪化させる再試行を避ける。

## やること

1. 通知から影響範囲を確認する。
2. 即介入が必要か、待機可能かを判断する。
3. 即介入が必要な場合だけ、runbook に定義された全体復旧手順へ進む。
4. task 単位の判断は、全体停止の意味を確認した後に行う。

## やってはいけないこと

- `human_review_required` と同じ感覚で個別 task だけを見ること
- 状況整理前に複数の強制操作を連打すること
- 障害範囲が不明なまま再試行すること

## このカードの成功条件

- 局所障害か全体障害かを誤読しない。
- 待機可否を短時間で決められる。
- 翌朝の自分が読んでも、なぜその判断をしたか説明できる。

検証:
- [../../reference/failure-injection.md](../../reference/failure-injection.md) §3.4 ToolRunner / Lease
- [../../reference/failure-injection.md](../../reference/failure-injection.md) §3.5 Reconcile / Restart

## このカードの失敗条件

- 個別 task の問題と誤認して対処する。
- 待機可能なのに深夜に過剰介入する。
- 全体停止の意味が分からず操作が拡散する。
