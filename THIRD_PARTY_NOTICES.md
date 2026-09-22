# Third-party source and assets

JEV-Star contains selected upstream runtime files and local JEV/Astra extensions.
The source manifest records the original paths, commits and file hashes.

| Component | Source | License information |
| --- | --- | --- |
| Macro Protoss base and action descriptions | [LLM Play SC2](https://github.com/histmeisah/Large-Language-Models-play-StarCraftII), commit `48564d6647c9f39a44d11588f2b9cbefee940fdd` | No repository-wide license file was present in the imported checkout. This repository does not relabel those files as MIT or Apache. |
| SMAC-Hard environment, opponent scripts and map assets | [SMAC-Hard](https://github.com/devindeng94/smac-hard), commit `48d77ec8ca2e2769fb81299f298ea438ce54fc9d` | Original [MIT license](licenses/SMAC-Hard-MIT.txt); imported from the upstream file named `LISCENCE`. |
| Vendored PySC2 runtime | PySC2 as included in that SMAC-Hard commit | Original copyright headers retained; [Apache License 2.0](licenses/PySC2-Apache-2.0.txt). |
| BurnySC2 | Installed from PyPI as `burnysc2==6.5.0` | External dependency; its package license applies. |
| NeurIPS style | `paper/neurips_2026.sty` | Original style file and its embedded notices retained. |
| StarCraft II maps and screenshots | Existing SC2/SMAC research assets | Original game and asset authors retain their rights. The game client is not distributed here. |

This initial consolidation does not assign a blanket license to all components.
See [source-manifest.json](docs/source-manifest.json) for provenance and the
[cleanup record](docs/repository-cleanup.md) for packaging changes.
