# オペレーションカード: human_review_required

出典: [personas.md](../../../personas.md) §3.1, §4.1, §7.3, §8.2, §10

関連参照:
- [../../reference/runbook.md](../../reference/runbook.md) §1.1 operator scope の正規化
- [../../reference/state-machine.md](../../reference/state-machine.md) §8 operator 向け参照
- [../../reference/failure-injection.md](../../reference/failure-injection.md) §3.4, §3.5

## このカードを開く場面

- task が `human_review_required` で止まったとき
- AI 暴走懸念や局所障害通知を受けたとき

補足:
- 本カードは task 局所停止のためのものであり、`system_degraded` や breaker open には使わない。

## 目的

task を安全に止めたまま、朝または PC 前の自分へ確実に引き継ぐ。

## まず確認すること

1. これは task 単位の停止か。
2. subprocess がまだ走っていないことを確認できるか。
3. いま必要なのは再開ではなく、停止維持と状況保存ではないか。

## この場面で優先すること

- 状況悪化を止める。
- 停止理由を失わない。
- 深い調査は PC に持ち越す。

## やること

1. task_id と停止理由を確認する。
2. まだ実行が続いていないかを確認する。
3. 今すぐ安全停止だけでよいなら、そのまま朝の精査に回す。
4. 即時の追加対応が必要な場合だけ、停止系または retry 系の手順を選ぶ。

## やってはいけないこと

- 停止理由を読まずに反射的に再試行すること
- 全体障害と誤認して system 系操作を行うこと
- 追加情報が無いまま複数の強制操作を重ねること

## このカードの成功条件

- task は止まったまま維持される。
- 停止理由と次の確認ポイントが残る。
- P1 が翌朝 PC で自然に調査を再開できる。

検証:
- [../../reference/failure-injection.md](../../reference/failure-injection.md) §3.4 ToolRunner / Lease
- [../../reference/failure-injection.md](../../reference/failure-injection.md) §3.5 Reconcile / Restart

## このカードの失敗条件

- 停止後も subprocess が動き続ける。
- 停止理由が曖昧で翌朝に再構成できない。
- 深夜の判断で無用な再試行を起こす。
