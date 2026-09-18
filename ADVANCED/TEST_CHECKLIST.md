# LocalFUT19 private beta 6 — incremental checklist

This checklist contains the beta 5 object-market validation plus the two
presentation corrections added in beta 6. Do not repeat the full beta 4
matrix unless one of these checks exposes a regression.

Use only the updated official EA App build, product version 19.0.4052077.0,
for this pass. FIFA 19 v1.0.0.0 is detected and isolated by the launcher, but
its feature-parity matrix has not been completed and is not part of this test.

For every failure record FAIL, the local time to the minute, the visible
screen and the newest .network.log and .jsonl files. Record PASS for each
completed section.

## Non-player Transfer Market

- [ ] Open **Consumables**, select **Player Training**, and search. Results are
      non-empty and every row is a real training consumable with a name and
      artwork; no player card or undefined card appears.
- [ ] Repeat with **Goalkeeper Training**.
- [ ] Search the available Contracts, Fitness and Healing filters. Each result
      remains in its selected family.
- [ ] Open **Club Items** and search Badges, Kits and Stadiums. Each available
      tab is non-empty and its results stay in that category.
- [ ] Open **Staff** and search. The small catalogue includes managers and
      staff cards such as coaches or physios; no player card appears.
- [ ] Quality, rarity and coin-price filters narrow results without changing
      their item family. Returning to the same search produces stable rows.
- [ ] Ball cards, staff-contract consumables and manager formation modifiers
      are absent. Their native card contracts are deliberately not enabled in
      this beta.

## Buy, assign and resell one item from each section

Perform this sequence once for a Consumable, once for a Club Item, and once
for a Manager or Staff card.

- [ ] Add the result to Transfer Targets, then use Buy Now.
- [ ] Coins are charged exactly once. Reopening the result or refreshing does
      not buy a second copy.
- [ ] The won object appears in **New Items / Unassigned** with the same name,
      type and artwork shown by the search result.
- [ ] Send it to the Club. It leaves New Items and appears under the matching
      My Club filter without a communication error.
- [ ] List the same tradeable object on the Transfer Market. It appears in the
      Transfer List with the same item type and legal starting/Buy Now prices.
- [ ] Close FIFA and the launcher normally, restart, and confirm that the Club,
      Transfer List, Transfer Targets and coin balance retained the result.

Do not perform these purchases from Player Search inside an SBC or Club squad
builder. That separate contextual player-return path is still under
investigation and is not changed by the object-market work.

## Regression smoke checks

- [ ] Search, buy and send one ordinary player to the Club. The existing player
      Transfer Market still works.
- [ ] Open one mixed Bronze or Silver pack, send one consumable to the Club and
      confirm that the pack animation and New Items flow still complete.

## Squad Battles club names

- [ ] Open Squad Battles Opponent Select. Every regular opponent displays a
      club name, never `Club 38`, `Club 52`, `Club 11`, `Club 10` or another
      numeric `Club <id>` fallback.
- [ ] When those four clubs appear, their labels are **SV Werder Bremen**,
      **Roma**, **Manchester United** and **Manchester City** respectively.
- [ ] Select one opponent and reach the difficulty screen. Its club name
      remains the same; starting a match is not required for this check.

## Josef Martínez SBC reward

- [ ] In SBC **Players**, open **Josef Martínez**. Its squad-rating
      requirement remains **Min. 84**.
- [ ] Reward Details identifies the reward as the **86 Award Winner** Josef
      Martínez card, not the incorrect 88 version.
- [ ] If the SBC is completed, the granted item is rating 86 and uses the
      Award Winner card design.

## Report completion

- [ ] Every row above has PASS or a timestamped FAIL.
- [ ] The newest completed .network.log and .jsonl files are included for any
      failure.
- [ ] No database, certificate, game executable, DLL, account token or raw
      Blaze payload is included in the report.
