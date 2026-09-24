# Changelog

## [0.9.1](https://github.com/chasef07/abita_s2s/compare/v0.9.0...v0.9.1) (2026-09-24)


### Bug Fixes

* prompt continued intake and evaluate conversational stalls ([#78](https://github.com/chasef07/abita_s2s/issues/78)) ([42075ea](https://github.com/chasef07/abita_s2s/commit/42075ea0aaf58d45660925190556f2caf489974f))

## [0.9.0](https://github.com/chasef07/abita_s2s/compare/v0.8.0...v0.9.0) (2026-09-24)


### Features

* add Jev scorecard and preserve partial evaluations ([#74](https://github.com/chasef07/abita_s2s/issues/74)) ([7b84dd1](https://github.com/chasef07/abita_s2s/commit/7b84dd15b17f399a461bff349e67f78b6e2abf67))
* **observability:** trace GPT-Live and export to Google Cloud ([#76](https://github.com/chasef07/abita_s2s/issues/76)) ([635ff55](https://github.com/chasef07/abita_s2s/commit/635ff559f604d43b947740fe27f642cff8a1adcf))

## [0.8.0](https://github.com/chasef07/abita_s2s/compare/v0.7.1...v0.8.0) (2026-09-24)


### Features

* **eligibility:** retain physician checks and link appointment evidence ([#72](https://github.com/chasef07/abita_s2s/issues/72)) ([2e84c0f](https://github.com/chasef07/abita_s2s/commit/2e84c0fd5f4eb159d1778ad1067e8428e49f36be))

## [0.7.1](https://github.com/chasef07/abita_s2s/compare/v0.7.0...v0.7.1) (2026-09-23)


### Bug Fixes

* support registering multiple new patients per call ([#70](https://github.com/chasef07/abita_s2s/issues/70)) ([75a19e8](https://github.com/chasef07/abita_s2s/commit/75a19e86e6d6a24b5d626ba6c02b478d7a915af2))

## [0.7.0](https://github.com/chasef07/abita_s2s/compare/v0.6.5...v0.7.0) (2026-09-23)


### Features

* add Jev evaluations and version agent component folders ([#57](https://github.com/chasef07/abita_s2s/issues/57)) ([9589652](https://github.com/chasef07/abita_s2s/commit/95896528171473f276fd47eb95e9c5b1b64ed937))

## [0.6.5](https://github.com/chasef07/abita_s2s/compare/v0.6.4...v0.6.5) (2026-09-22)


### Bug Fixes

* log OpenAI live session IDs for call correlation ([#62](https://github.com/chasef07/abita_s2s/issues/62)) ([3acdeee](https://github.com/chasef07/abita_s2s/commit/3acdeee3dffa742712aec759e1aec66e47fb889e))
* remove GPT-4o-mini post-call judges ([#63](https://github.com/chasef07/abita_s2s/issues/63)) ([8dcf439](https://github.com/chasef07/abita_s2s/commit/8dcf43908eeeb3273edda79af6995f7bd0ef26e4))
* **scheduling:** distinguish confirmed appointment slots ([#66](https://github.com/chasef07/abita_s2s/issues/66)) ([3f0f41a](https://github.com/chasef07/abita_s2s/commit/3f0f41a9e86c0293759f52b4e6ae4e729bd4f6d3))

## [0.6.4](https://github.com/chasef07/abita_s2s/compare/v0.6.3...v0.6.4) (2026-09-22)


### Bug Fixes

* clarify speaker call completion prompt ([#58](https://github.com/chasef07/abita_s2s/issues/58)) ([9bc03fe](https://github.com/chasef07/abita_s2s/commit/9bc03fed1ae0ec0653f0afc70023d057b5a71f1c))
* prewarm AnyIO socket imports before calls ([#60](https://github.com/chasef07/abita_s2s/issues/60)) ([514213e](https://github.com/chasef07/abita_s2s/commit/514213e8f76ef74e6dc12fdefcd1ed16dcbbdf0d))
* remove unsupported transfer interruption override ([#61](https://github.com/chasef07/abita_s2s/issues/61)) ([51feb10](https://github.com/chasef07/abita_s2s/commit/51feb101c57ee2855f54a67bffadb5c1416cf157))

## [0.6.3](https://github.com/chasef07/abita_s2s/compare/v0.6.2...v0.6.3) (2026-09-21)


### Bug Fixes

* preserve completed registration state for scheduling ([#54](https://github.com/chasef07/abita_s2s/issues/54)) ([7bacbff](https://github.com/chasef07/abita_s2s/commit/7bacbff08eb75fa1d1752bc3a932a171e458258f))

## [0.6.2](https://github.com/chasef07/abita_s2s/compare/v0.6.1...v0.6.2) (2026-09-20)


### Bug Fixes

* consume middleware-owned insurance and scheduling outcomes ([#52](https://github.com/chasef07/abita_s2s/issues/52)) ([d3a5bf6](https://github.com/chasef07/abita_s2s/commit/d3a5bf6abe67dfc5467c98707d81cb58a9140e76))

## [0.6.1](https://github.com/chasef07/abita_s2s/compare/v0.6.0...v0.6.1) (2026-09-20)


### Bug Fixes

* fence stale call operations and recover transfer deadlines ([#49](https://github.com/chasef07/abita_s2s/issues/49)) ([08f838a](https://github.com/chasef07/abita_s2s/commit/08f838ab2484213e1e937e86e9694e1b5f5c401c))
* **scheduling:** use chart insurance for existing patients ([#48](https://github.com/chasef07/abita_s2s/issues/48)) ([c7c4ef1](https://github.com/chasef07/abita_s2s/commit/c7c4ef149f99927b8edf30952087df468b718351))

## [0.6.0](https://github.com/chasef07/abita_s2s/compare/v0.5.3...v0.6.0) (2026-09-20)


### Features

* add warm time-aware office greetings ([#46](https://github.com/chasef07/abita_s2s/issues/46)) ([1dd7467](https://github.com/chasef07/abita_s2s/commit/1dd7467f8b4fe1121cd04ac171853c9cb296141d))

## [0.5.3](https://github.com/chasef07/abita_s2s/compare/v0.5.2...v0.5.3) (2026-09-18)


### Bug Fixes

* **scheduling:** remove dedicated hospital intake fields ([#42](https://github.com/chasef07/abita_s2s/issues/42)) ([50982ed](https://github.com/chasef07/abita_s2s/commit/50982ed6eb7495a35ec1f9af975b5b66a4a739bb))
* simplify tool contracts and bound availability retries ([#41](https://github.com/chasef07/abita_s2s/issues/41)) ([5228b3d](https://github.com/chasef07/abita_s2s/commit/5228b3d93d65347fd228007b4856c921f4c7f654))

## [0.5.2](https://github.com/chasef07/abita_s2s/compare/v0.5.1...v0.5.2) (2026-09-18)


### Bug Fixes

* clarify appointment availability responses ([#39](https://github.com/chasef07/abita_s2s/issues/39)) ([d3a8f93](https://github.com/chasef07/abita_s2s/commit/d3a8f9318b6bd36b49e98a50c67a37c46ef5e1ea))
* **insurance:** use acceptance as registration permission ([#38](https://github.com/chasef07/abita_s2s/issues/38)) ([460380a](https://github.com/chasef07/abita_s2s/commit/460380a952685cdeecbadeaf4bf472a908414355))

## [0.5.1](https://github.com/chasef07/abita_s2s/compare/v0.5.0...v0.5.1) (2026-09-18)


### Bug Fixes

* simplify new patient intake and silence internal narration ([#36](https://github.com/chasef07/abita_s2s/issues/36)) ([eb6e5e9](https://github.com/chasef07/abita_s2s/commit/eb6e5e9494db78e79c4fee898445274b536578cc))

## [0.5.0](https://github.com/chasef07/abita_s2s/compare/v0.4.1...v0.5.0) (2026-09-17)


### Features

* **insurance:** consume authoritative middleware decisions ([#34](https://github.com/chasef07/abita_s2s/issues/34)) ([5b9f75e](https://github.com/chasef07/abita_s2s/commit/5b9f75eb880f1c85e6c80d58ece0a0b4f4d26e57))


### Bug Fixes

* **scheduling:** consume middleware metadata and reschedule receipts ([#33](https://github.com/chasef07/abita_s2s/issues/33)) ([2e87765](https://github.com/chasef07/abita_s2s/commit/2e877651bb3f08a4410afae0d2712fe27eea2834))

## [0.4.1](https://github.com/chasef07/abita_s2s/compare/v0.4.0...v0.4.1) (2026-09-17)


### Bug Fixes

* **identity:** consume middleware-owned patient resolution ([#31](https://github.com/chasef07/abita_s2s/issues/31)) ([2c955da](https://github.com/chasef07/abita_s2s/commit/2c955dafe5b9695e959ff46c7fe56b01f3b6d170))

## [0.4.0](https://github.com/chasef07/abita_s2s/compare/v0.3.6...v0.4.0) (2026-09-17)


### Features

* **evals:** judge production calls with built-in LiveKit judges ([#28](https://github.com/chasef07/abita_s2s/issues/28)) ([8de3e4d](https://github.com/chasef07/abita_s2s/commit/8de3e4d648d5aae6c27359887391f0f9eca9bd14))


### Bug Fixes

* correct booking replay and streamline Sofia call startup ([#30](https://github.com/chasef07/abita_s2s/issues/30)) ([87dd8f9](https://github.com/chasef07/abita_s2s/commit/87dd8f9878ba24dcb4f60035fe12c0a1b26e8574))

## [0.3.6](https://github.com/chasef07/abita_s2s/compare/v0.3.5...v0.3.6) (2026-09-16)


### Bug Fixes

* **scheduling:** align appointment workflows and tool results ([#24](https://github.com/chasef07/abita_s2s/issues/24)) ([8ecbd81](https://github.com/chasef07/abita_s2s/commit/8ecbd81f08c3df3d4454eaeb7c49d6928c880160))
* simplify patient lookup and scheduling ownership ([#26](https://github.com/chasef07/abita_s2s/issues/26)) ([67487bc](https://github.com/chasef07/abita_s2s/commit/67487bcb79513d0c1dd061c50af8b95c9d6d44bf))

## [0.3.5](https://github.com/chasef07/abita_s2s/compare/v0.3.4...v0.3.5) (2026-09-16)


### Bug Fixes

* clarify office knowledge workflow and tool responses ([#23](https://github.com/chasef07/abita_s2s/issues/23)) ([d769aba](https://github.com/chasef07/abita_s2s/commit/d769aba67c4a3fb03ff559961823c74c6b0208e7))
* refine thinker prompt for one-offer staff transfers ([#21](https://github.com/chasef07/abita_s2s/issues/21)) ([b0eeefd](https://github.com/chasef07/abita_s2s/commit/b0eeefd41def2c794f0089a3bd587256c0254c49))

## [0.3.4](https://github.com/chasef07/abita_s2s/compare/v0.3.3...v0.3.4) (2026-09-16)


### Bug Fixes

* align availability contracts and simplify voice workflows ([#20](https://github.com/chasef07/abita_s2s/issues/20)) ([ff3dce3](https://github.com/chasef07/abita_s2s/commit/ff3dce3685e8c0d80ef047a3b17f073dd84f05f6))
* **patient:** use phone lookup context for existing patient resolution ([#18](https://github.com/chasef07/abita_s2s/issues/18)) ([efb0720](https://github.com/chasef07/abita_s2s/commit/efb07206d8eb3e3cb90c9962262a95d784ccca09))

## [0.3.3](https://github.com/chasef07/abita_s2s/compare/v0.3.2...v0.3.3) (2026-09-15)


### Bug Fixes

* version prompt and eval bundles only when content changes ([#16](https://github.com/chasef07/abita_s2s/issues/16)) ([2c81d0a](https://github.com/chasef07/abita_s2s/commit/2c81d0a4a99f1cb6ff9fabe6df089f871a86a04d))

## [0.3.2](https://github.com/chasef07/abita_s2s/compare/v0.3.1...v0.3.2) (2026-09-15)


### Bug Fixes

* use relative LiveKit deployment config path ([dcf412a](https://github.com/chasef07/abita_s2s/commit/dcf412a8f659c282258f3fa99198083521e17e14))

## [0.3.1](https://github.com/chasef07/abita_s2s/compare/v0.3.0...v0.3.1) (2026-09-15)


### Bug Fixes

* explicitly select LiveKit deployment agent ([c3f6d91](https://github.com/chasef07/abita_s2s/commit/c3f6d91cd9ab671160bfbd0668d182e357e344df))

## [0.3.0](https://github.com/chasef07/abita_s2s/compare/v0.2.0...v0.3.0) (2026-09-15)


### Features

* add authenticated staff task delivery ([d6cbdf8](https://github.com/chasef07/abita_s2s/commit/d6cbdf87331de35525eb659f2285fb9bba0935d0))
* add first-name and DOB patient resolution ([d651920](https://github.com/chasef07/abita_s2s/commit/d651920159118b9ef8d5d7c6c604ad8a1b434bb4))
* add LiveKit releases and native call reporting ([#10](https://github.com/chasef07/abita_s2s/issues/10)) ([4d0ab87](https://github.com/chasef07/abita_s2s/commit/4d0ab8770bcf3071903a8530b84f1d93601d155e))
* add Product-backed office knowledge search ([7cc16cb](https://github.com/chasef07/abita_s2s/commit/7cc16cb4959f0bf5b79e983b3aea34c3ef418f0d))
* add production office routing and profile-based greetings ([29c684c](https://github.com/chasef07/abita_s2s/commit/29c684c694b2437382ad9a0351b04bdfbae59259))
* add typed call state and patient lookup guards ([cb2634e](https://github.com/chasef07/abita_s2s/commit/cb2634e30bdffa9656d1dea172e86c3aafbfbcc8))
* define identity-bound insurance acceptance contract ([e9a3512](https://github.com/chasef07/abita_s2s/commit/e9a3512e56650b1e646bd26195114089cbef41ff))
* migrate appointment scheduling with safe mutation receipts ([2c0fc69](https://github.com/chasef07/abita_s2s/commit/2c0fc69bb3ff47891e90dbd51317d6e7bf846803))
* migrate appointment scheduling with safe mutation receipts ([2c0fc69](https://github.com/chasef07/abita_s2s/commit/2c0fc69bb3ff47891e90dbd51317d6e7bf846803))
* migrate appointment scheduling with safe mutation receipts ([6220bdf](https://github.com/chasef07/abita_s2s/commit/6220bdf66eb23f47eb3f1b5beec2bf8ac6c87eef))
* migrate insurance checks and patient registration ([a7687e4](https://github.com/chasef07/abita_s2s/commit/a7687e4e4ff0c840651ccee4cfbe238efbe2dda7))
* migrate insurance checks and patient registration ([a7687e4](https://github.com/chasef07/abita_s2s/commit/a7687e4e4ff0c840651ccee4cfbe238efbe2dda7))
* migrate insurance checks and patient registration safely ([392d945](https://github.com/chasef07/abita_s2s/commit/392d945a7b30571279485f731d90fe2f1f3d0b2a))
* migrate safe call transfer and completion tools ([028f460](https://github.com/chasef07/abita_s2s/commit/028f46045e74f1cbdb152b32f3906305ded36d2f))


### Bug Fixes

* preserve handoff configuration and returning-patient scheduling ([#7](https://github.com/chasef07/abita_s2s/issues/7)) ([4296f3b](https://github.com/chasef07/abita_s2s/commit/4296f3b74dc26dbb879c12e5b58408f1e8ee31e3))
* refresh insurance references before subsequent changes ([a051251](https://github.com/chasef07/abita_s2s/commit/a05125137e7c756f07d6a9e5458fc4dcd3156095))
* require verified visit type before rescheduling ([74b0107](https://github.com/chasef07/abita_s2s/commit/74b0107e545306ef51a6cf59d6a60ea97b37605d))
