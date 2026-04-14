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
- backup / restore の成否を自分で確認できる。
- retention や rotation が、実運用を壊さずに回る。
- 「いま触っていいもの」と「不用意に触ると危険なもの」が分かる。

## 成功

- runbook 通りに upgrade / restore drill が完走する。
- 保守後に通常運用が影響を受けていないと確信できる。
- secret rotation 後、再発行や影響範囲の追跡ができる。
- backup 成功判定が明示的に残る。

## 失敗

- upgrade 手順で task truth を壊す。
- restore drill の手順が実運用と乖離していて通らない。
- retention worker が task 実行と競合する。
- 触った結果、戻し方が分からない。

## 代表 utterance

- 「これ触って大丈夫?」
- 「リストアドリル、今月やった?」
- 「secret 更新したけどどう確認するんだっけ」

## 設計含意

- runbook と restore drill は一過性の資料ではなく、繰り返し実行できる形で維持する。
- backup / replay / rebuild / retention は task 実行系と責務を分離する。
- secret rotation や upgrade は、真実源を壊さず段階的に実施できる設計を優先する。

## 関連シナリオ

- マスター §7.6 継続保守
