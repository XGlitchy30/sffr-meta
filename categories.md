# Restriction categories

The vocabulary of the `category` column in `restrictions.csv` and `verdicts.csv`.
`tools/validate_meta.py` reads the labels from the table below, so adding a row here is
all it takes to add a category. Labels are lower case, digits and hyphens.

|Label|Restriction is based on…|Definition|
|-|-|-|
|`power`|**Intrinsic strength**|The card's effects are individually too efficient or impactful for the format, irrespective of any particular combo, archetype, or usage pattern.|
|`consistency`|**Reliability**|The card makes a strategy reach its intended game state too reliably, primarily by increasing access to desired cards or lines.|
|`genericity`|**Applicability**|The card is too broadly usable across otherwise unrelated decks, making its power insufficiently dependent on deck identity.|
|`synergy`|**Interaction with specific cards**|The card becomes problematic because of a particular interaction with one or more other cards, rather than because of its standalone effect.|
|`engine`|**Engine construction**|The card forms part of a compact package that produces disproportionate value, even though the individual card may be reasonable in isolation.|
|`lock`|**Inability to perform game actions**|The card establishes or contributes to a game state in which a defined set of actions is unavailable to the opponent.|
|`resource-denial`|**Loss of accumulated resources**|The card's primary problem is repeatedly stripping cards, cards in hand, field resources, graveyard resources, or equivalent accumulated assets.|
|`recursion`|**Repeatable value generation**|The card can repeatedly reuse itself or other resources in a manner judged excessive, without requiring an infinite loop.|
|`loop`|**Indefinite repetition**|The card enables an actual infinite or functionally unbounded sequence of game actions, resources, summons, damage, etc.|
|`ftk`|**First-turn victory**|The card directly enables or materially contributes to a strategy that wins before the opponent receives a meaningful turn.|
|`otk`|**Single-turn lethality**|The card makes a one-turn kill excessively accessible or reliable during a normal game state.|
|`stall`|**Game prolongation**|The card's primary problem is preventing the game from reaching a conclusion at an acceptable rate rather than preventing specific game actions.|
|`variance`|**Random outcome**|The card introduces an unacceptable amount of randomness or swinginess, such that outcomes depend excessively on chance.|
|`nontermination`|**Failure to reach a game conclusion**|The card creates games or game states that can continue indefinitely or become practically impossible to finish.|
|`metagame`|**Metagame concentration**|The restriction responds to the prevalence or dominance of a strategy/deck in the overall format rather than to the card's intrinsic characteristics.|
|`diversity`|**Loss of strategic variety**|The card suppresses alternative strategies by making a narrow range of deckbuilding choices disproportionately mandatory or viable.|
|`character`|**Format incompatibility**|The card is restricted because its mechanics or gameplay are contrary to the intended philosophy or identity of the custom format, independent of measured power.|
|`rules`|**Rules burden**|The restriction exists because of ruling ambiguity, rules interactions, implementation problems.|
|`preemptive`|**Anticipation of future problems**|The card is restricted before the problem has actually manifested in the format, based on testing, expected interactions, or prior evidence.|
|`collateral`|**Indirect targeting of another problem**|The card is restricted primarily because limiting it is a means of weakening a different card, deck, engine, or interaction.|
|`external`|**Non-gameplay purpose**|The card is on the list for a technical or administrative reason (for example a utility card for the simulator) rather than for regular play, so its status says nothing about its power.|
|`unknown`|**Insufficient evidence**|There is not enough information to identify the actual rationale for the restriction.|
