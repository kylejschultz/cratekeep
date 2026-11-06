# Changelog

## [1.1.0](https://github.com/kylejschultz/cratekeep/compare/v1.0.1...v1.1.0) (2026-11-06)


### Features

* add All Inboxes view with source-aware actions ([9b8ce91](https://github.com/kylejschultz/cratekeep/commit/9b8ce91b2ff7a8dccb1d69842a82d0e097d41a81))
* add per-Inbox discovery and import controls ([39963ea](https://github.com/kylejschultz/cratekeep/commit/39963eabf067e65a49cc11d62053527669373083))
* add per-inbox discovery modes and live arrivals ([e0dda5b](https://github.com/kylejschultz/cratekeep/commit/e0dda5b795687276260ab085932cf157be96c0c3))
* browse and remember album file locations ([28a60a7](https://github.com/kylejschultz/cratekeep/commit/28a60a7308bf72825e56f9a39841da9d6d432606))
* clear imported inbox originals from results ([3f6d0df](https://github.com/kylejschultz/cratekeep/commit/3f6d0df4a9edb15d4795678df80f031d20e963e2))
* delete inbox albums and tracks without importing ([825bb0a](https://github.com/kylejschultz/cratekeep/commit/825bb0abffbc63b90bc9171d5cab44396a76282b))
* edit artist credits and organize library files ([#26](https://github.com/kylejschultz/cratekeep/issues/26)) ([838943b](https://github.com/kylejschultz/cratekeep/commit/838943bf0bc8442caf615de1561be6c1945ff68c))
* group activity history with timestamps and persistent clear controls ([9ce388f](https://github.com/kylejschultz/cratekeep/commit/9ce388f8e1c533142d1a9de55e3f8c8f66f7b215))
* invite users to share optional Beacon analytics ([b4e2b9a](https://github.com/kylejschultz/cratekeep/commit/b4e2b9a6c4dbd7907d1c505165addbd9b4baad98))
* prepare Cratekeep v1.1 for integration ([33c2ee5](https://github.com/kylejschultz/cratekeep/commit/33c2ee5374e6cdcbe4359b667172fabc19d0d34a))
* prioritize actionable activity and quiet successful checks ([c9a86a6](https://github.com/kylejschultz/cratekeep/commit/c9a86a62bcc0e27d3cbca1073ecbdd048dcd42b7))
* review album and track metadata in a dedicated editor ([6a2c707](https://github.com/kylejschultz/cratekeep/commit/6a2c707343d4de6bec2500650c3a998dec7707c4))
* track background processes in shared activity monitor ([b2be0a9](https://github.com/kylejschultz/cratekeep/commit/b2be0a9b7e9c4d6e751853826b5680647d6f9030))
* use compact header icons with running activity indicator ([1684807](https://github.com/kylejschultz/cratekeep/commit/1684807a4efce08a4d9a57e01dfa85f9e1cbe95e))


### Bug Fixes

* accept track-only metadata drafts for review ([acd6623](https://github.com/kylejschultz/cratekeep/commit/acd662392690e611b69d67926a6f046e5b3bf361))
* clear accompanying files from imported inbox folders ([9f8475e](https://github.com/kylejschultz/cratekeep/commit/9f8475ea65cef4a0702b8cacb7a2f668a37b4306))
* clear transient matching warnings after successful retry ([78beda2](https://github.com/kylejschultz/cratekeep/commit/78beda244087df2b6715f58c1df5137b95760974))
* coalesce routine inbox checks and separate new arrivals ([5cc5918](https://github.com/kylejschultz/cratekeep/commit/5cc5918093f2bbeef272aaf996cc8d8b2159424c))
* confirm Inbox originals removal in the album sidebar ([daf86c3](https://github.com/kylejschultz/cratekeep/commit/daf86c3ab6fc48b22036bebfdc846b1ecbfd3414))
* discover settled Inbox albums during neighboring downloads ([ccd938c](https://github.com/kylejschultz/cratekeep/commit/ccd938ce4f89be81382381411eb52531546bcab5))
* distinguish track totals from match coverage ([6b0897e](https://github.com/kylejschultz/cratekeep/commit/6b0897e39f403e1e34096d338cf5d30274b831c9))
* edit artist credit lists and synchronize related tags ([23890a3](https://github.com/kylejschultz/cratekeep/commit/23890a3aa33397e23a33ae27989a5733a77ac7c5))
* follow Settings hash links without reloading ([27674e2](https://github.com/kylejschultz/cratekeep/commit/27674e2d73399e9d543de39bd190a47388475009))
* group inbox editing in a responsive form ([e0bdbaf](https://github.com/kylejschultz/cratekeep/commit/e0bdbaf84eec00752b5cfae603e78458209ded37))
* handle duplicate import cleanup receipts in Activity ([f72da14](https://github.com/kylejschultz/cratekeep/commit/f72da1402a7d89e033cefe922a5588372f194843))
* include match details in activity completion updates ([85d921c](https://github.com/kylejschultz/cratekeep/commit/85d921cb4a05be7ff8c395d12cb9118fbc929290))
* integrate activity with accepted metadata and location editor ([0c4f660](https://github.com/kylejschultz/cratekeep/commit/0c4f6601b4d341eb63f25ec221296ea1bc7ce189))
* keep Activity compact and progress in its owning inspector ([ce4ce35](https://github.com/kylejschultz/cratekeep/commit/ce4ce358f94f4522ce15c976bfc59ee2b18907a3))
* keep activity payloads bounded and verify background completion ([4ffc2f2](https://github.com/kylejschultz/cratekeep/commit/4ffc2f236996aa654f7881bd451bb3b54a2273cd))
* keep bulk import previews within activity details ([2ff1e91](https://github.com/kylejschultz/cratekeep/commit/2ff1e912af92abdbe15afe3c87a2f413baafa5ff))
* keep import errors visible beside retry ([ccd9742](https://github.com/kylejschultz/cratekeep/commit/ccd9742019508c5088a11b7bb2c0aabe2caf5e5e))
* keep Inbox albums visible during slow scans ([e31a2a4](https://github.com/kylejschultz/cratekeep/commit/e31a2a489bdcf21480bd42ae08f078eee9983dc3))
* keep refresh counts cumulative and compact activity summaries ([718d52a](https://github.com/kylejschultz/cratekeep/commit/718d52a63e5e16676759eaefe91d33ae06d727d6))
* pair credited emoji track titles using base title evidence ([315de47](https://github.com/kylejschultz/cratekeep/commit/315de47be8f432859357f632e48e714dfa2970a1))
* pair symbol titles using track position and duration ([1e4d53b](https://github.com/kylejschultz/cratekeep/commit/1e4d53b2b2f402f8758669d55d518e3bae6a9caf))
* place automatic matching beside each inbox ([b7000f7](https://github.com/kylejschultz/cratekeep/commit/b7000f7596ddc732e81519cfddd2ea2b02424a45))
* polish setup and library removal activity for v1.1 ([4cfec56](https://github.com/kylejschultz/cratekeep/commit/4cfec56149d3afa7fa55a3e2e81ce2a5ed7555e5))
* preserve MusicBrainz release track title qualifiers ([4cf8332](https://github.com/kylejschultz/cratekeep/commit/4cf8332c70cef32d0643f4dbbfe2fed49f208b27))
* read activity snapshots consistently during scan compaction ([2d0f044](https://github.com/kylejschultz/cratekeep/commit/2d0f04400ede8e27a8a285952931fb6aca6d6f2b))
* refresh selected albums after background matching ([8ef4748](https://github.com/kylejschultz/cratekeep/commit/8ef4748031cec810a95c29eea98806b08314cf84))
* report actual phases of file operations ([32f8a7b](https://github.com/kylejschultz/cratekeep/commit/32f8a7bb7407933a7f2d11a58c1ee89879e7e0dc))
* report duplicate import progress by phase ([f2cafbf](https://github.com/kylejschultz/cratekeep/commit/f2cafbf326573a53c3eb4d4c268faaab0073d490))
* report lost instant inbox roots promptly ([ef1e115](https://github.com/kylejschultz/cratekeep/commit/ef1e11522cc17ed1be6160baa18bcd470c142b1f))
* report saved Inbox match reuse consistently ([8a11edc](https://github.com/kylejschultz/cratekeep/commit/8a11edc70c75d3d4e053acfe6957ef9ac4663619))
* restore metadata editor focus to its trigger ([c308e6c](https://github.com/kylejschultz/cratekeep/commit/c308e6ce0d6ab474ec39e7a676380f72a1b979ce))
* retain track names in inbox deletion confirmation ([df73c49](https://github.com/kylejschultz/cratekeep/commit/df73c4987b32a03705241d0ee5bf86bbc25cbddf))
* show delayed progress within operation dialogs ([10f8955](https://github.com/kylejschultz/cratekeep/commit/10f8955350b166cac017f1facaaf917b6e564df8))
* show discovery reconnection status after repeated failures ([3e33f54](https://github.com/kylejschultz/cratekeep/commit/3e33f541803c3e8b8a4f5375862500f39778702e))
* simplify inbox deletion confirmation ([cf1720b](https://github.com/kylejschultz/cratekeep/commit/cf1720b1a9937dc16a824df0f65595022b872ceb))
* summarize inbox preferences outside the editor ([f1a0581](https://github.com/kylejschultz/cratekeep/commit/f1a0581548dff84af80a1f4cc84487e419bf75e8))
* surface required duplicate decisions before import ([a678cb9](https://github.com/kylejschultz/cratekeep/commit/a678cb9182de7256cf79bc856e93039b11a4ff06))
* synchronize artist tags and metadata-driven paths ([8cc2914](https://github.com/kylejschultz/cratekeep/commit/8cc2914fbfadb989a0d5ddb36a62b1dc0eff4b42))
* warn instead of blocking metadata mismatches on import ([60ab8f4](https://github.com/kylejschultz/cratekeep/commit/60ab8f4b93f29c4a369043fd45541d3a6832fe6e))

## [1.0.1](https://github.com/kylejschultz/cratekeep/compare/v1.0.0...v1.0.1) (2026-10-27)


### Bug Fixes

* promote Inbox discovery and setup fixes for v1.0.1 ([#19](https://github.com/kylejschultz/cratekeep/issues/19)) ([94f796d](https://github.com/kylejschultz/cratekeep/commit/94f796d7bc9b6bbbe3138f228854dddd8ce8cf8e))

## 1.0.0 (2026-10-26)


### Features

* initial Cratekeep implementation ([#11](https://github.com/kylejschultz/cratekeep/issues/11)) ([95a6eda](https://github.com/kylejschultz/cratekeep/commit/95a6edac2427b0971f77ba68d6d44081a7efbd45))
* promote Cratekeep to release for v1.0.0 ([cd2aadd](https://github.com/kylejschultz/cratekeep/commit/cd2aadde2a4b33ea974b77d30b500c930d4f3a58))


### Bug Fixes

* recognize component Release Please branch ([#15](https://github.com/kylejschultz/cratekeep/issues/15)) ([7aa7a7e](https://github.com/kylejschultz/cratekeep/commit/7aa7a7e1594d1d95e2bd169a9447c0b3937c4361))
