# P4: 継続保守を行うメンテナとしての自分 (Secondary)

> マスター: `../personas.md` §4.3

## 一言サマリ

新しいバージョンへの更新、secret 差し替え、restore drill、retention 確認を担当する自分。普段は触らないが、壊れると困る基盤部分を時々保守する。

## 場面 / Device / Surface

- 場所: デスク、計画的な保守時間。
- Device: PC。
- Surface: runner-cli、Forgejo admin、systemd、Litestream、sops + age。

## 認知予算

- 1 保守セッション 30 分〜数時間。
- 割り込みではなく計画作業。
- runbook を参照しながら実行する。

## 最重要ニーズ

- upgrade 手順が短く、壊してはいけない境界が明確である。
- backup / restore の成否を、手書きメモに頼らず確認できる。
- retention や rotation が、実運用を壊さずに回る。
- 「いま触っていいもの」と「不用意に触ると危険なもの」が分かる。

## 設計含意

- runbook と restore drill は一過性の資料ではなく、繰り返し実行できる形で維持する。
- backup / replay / rebuild / retention は task 実行系と責務を分離する。
- secret rotation や upgrade は、真実源を壊さず段階的に実施できる設計を優先する。
- 保守の成否は、手書きメモではなくシステム状態から確認できるようにする。

## 行動特性

- 保守は計画作業としてまとめて実施する。
- runbook を見ながら進め、前回の記憶には依存しない。
- 危険な境界を先に確認してから操作する。
- 作業後の成功判定が明示されていないと不安になる。

## 成功

- upgrade や restore drill を runbook どおり反復実行できる。
- backup / replay / rebuild / retention の境界が明確で、手順を誤りにくい。
- 保守作業後に、安全に運用継続できるかをシステム状態から確認できる。
- secret rotation や retention 設定変更が task 実行系を壊さない。

## 失敗

- 保守手順が属人的で、システム状態だけでは成否を確認できない。
- restore、rebuild、replay の責務が曖昧で、復旧時に迷う。
- retention や rotation が実行系に副作用を与える。
- upgrade や secret 更新のたびに「壊すかもしれない」不安が強い。

## 代表 utterance

- 「これ触って大丈夫?」
- 「リストアドリル、今月やった?」
- 「secret 更新したけどどう確認するんだっけ」

## 関連シナリオ

- マスター §7.6 継続保守
