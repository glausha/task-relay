# task-relay ペルソナカード

マスター: `../personas.md`

本ディレクトリは、マスター文書と対になるペルソナ単票カード集である。
設計会話で短時間参照するための簡易版として維持する。深い議論や横断原則は必ずマスターを参照する。

## ペルソナ一覧

| ID | カード | 優先度 | 中核ニーズ |
|---|---|---|---|
| P1 | [P1-mobile-developer.md](P1-mobile-developer.md) | Primary | モバイル併用で状況把握と最小介入 |
| P2 | [P2-night-ops.md](P2-night-ops.md) | Secondary | 深夜障害時の即時停止・待機・再開判断 |
| P3 | [P3-auditor.md](P3-auditor.md) | Secondary | 事後の経緯再構成と再発防止 |
| P4 | [P4-maintainer.md](P4-maintainer.md) | Secondary | upgrade / backup / restore / secret 更新の安全な反復 |

## 使い方

- 設計会話の冒頭で「このペルソナのこの指標を満たす」と宣言する。
- 複数ペルソナが衝突する場合、まず `../personas.md` の裁定順に従う。secondary 間では概ね P2 > P4 > P3 だが、常に P1 の目標達成を優先する。
- 本カードは persona の人間像だけを扱う。DB スキーマや failure_code enum はマスターにもカードにも持ち込まない。それらは `../basic-design-v1.0.md` / `../detailed-design-v1.0.md` 側で扱う。

## 非ペルソナ

- 複数承認者 / 常時監視ユーザー / 完全放任ユーザーは `../personas.md` §5 参照。
