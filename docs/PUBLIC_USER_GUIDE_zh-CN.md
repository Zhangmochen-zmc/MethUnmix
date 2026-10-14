# MethUnMix 用户指南——1.0.0 修复后本地候选

> 状态：**尚未发布的修复后本地候选。** 本文适用于
> `fixed-1.0.0-candidate.1-20261014`，不适用于现有直链中不同字节的旧
> 1.0.0 wheel；它不是稳定发行公告、科学验证通过结论或 READY/RU 状态变更。

MethUnMix 是面向 DNA 甲基化解卷积的离线优先控制层：它选择路线、检查用户提供
的输入与外置资产、调用满足条件的 workflow，并记录 provenance。core 不包含
reference archive、SIF/container、模型权重、genome cache、原始输入或第三方算法实现。

## 安装 core

仅安装 owner 提供且 SHA256 与候选证据清单一致的精确 artifact；不得用该候选覆盖
现有公开 1.0.0 URL 的字节。

```bash
python3 -m venv /absolute/path/to/methunmix-fixed-1.0.0-venv
/absolute/path/to/methunmix-fixed-1.0.0-venv/bin/python -m pip install \
  --no-deps /absolute/path/to/fixed-candidate/methunmix-1.0.0-py3-none-any.whl
/absolute/path/to/methunmix-fixed-1.0.0-venv/bin/methunmix --version
```

本候选只安装 `methunmix` 这一项公开命令。历史 `demethflow_core` import 路径和
`demethflow-*` schema 名仅作为内部兼容身份保留，不应作为第二个 CLI 对外说明。

问题报告应保留 release manifest 中的 artifact SHA256 和安装后的
`execution_content_identity`。前者标识归档字节，后者标识实际安装的 core 与随包
执行资源，二者不能互相代替。

## 无模型快速体验

以下命令只读取随包 adapter 描述符；不会下载资产、启动 container、调用 Nextflow
或运行解卷积算法。

```bash
methunmix adapter validate --json
methunmix adapter list --json
```

`status: PASS` 只表示描述符 schema 合规，不表示 workflow、runtime、准确度或科学
状态通过。

## 审计并安装当前公开 reference catalog

当前不可变 catalog 为：

```text
https://download.cncb.ac.cn/methbank/MethUnMix/reference/1.0.0/catalog/methunmix-reference-1.0.0-owner-rights-public-safe-candidate.6/catalog.json
字节数：528233
SHA256：7ef6600563a0c182f14593ce1236b95558d0a860dfde22d282364b0e0cd805f2
```

它将 33/33 个唯一 selector 配置为 `PUBLIC_DOWNLOADABLE`。历史证据覆盖全部 33 个
archive 字节；candidate.6 闭环仅重新通过安装版客户端 fetch/import/verify 了一个
代表对象，并未重新下载全部 33 项。core 不会自动发现该 URL，必须显式提供：

```bash
METHUNMIX=/absolute/path/to/methunmix-fixed-1.0.0-venv/bin/methunmix
ASSET_HOME=/absolute/path/to/methunmix-assets
METADATA=/absolute/path/to/catalog-metadata
CATALOG_URL=https://download.cncb.ac.cn/methbank/MethUnMix/reference/1.0.0/catalog/methunmix-reference-1.0.0-owner-rights-public-safe-candidate.6/catalog.json

"$METHUNMIX" --home "$ASSET_HOME" asset audit --online \
  --catalog-url "$CATALOG_URL" --metadata-dir "$METADATA"
"$METHUNMIX" --home "$ASSET_HOME" reference available \
  --metadata-dir "$METADATA"
"$METHUNMIX" --home "$ASSET_HOME" reference fetch --install \
  --metadata-dir "$METADATA" builtin.brain.450k@1.0.0
"$METHUNMIX" --home "$ASSET_HOME" asset verify \
  reference.builtin.brain.450k@1.0.0 --json
```

必须使用完整版本 selector。reference 校验成功不能替代 runtime、SIF、model、cache
或精确本地依赖 binding。

## 真实分析前

真实任务必须明确 scenario、输入平台、route、精确 reference selector、工具、
profile/device、适用 seed 以及一个新输出目录。`asset plan/import/verify` 管理外置
资产；`plan` 解释选择；`doctor` 重新检查当前输入和环境；只有 `run` 会创建正常
workflow。完整配置下的 `plan` 或 `doctor` 可能做有界 runtime/container smoke，
不能把它们当成“绝不启动 container”的命令。

Array-native 450K/EPIC 的展示 build 为 `not_applicable`；native WGBS 必须声明 hg19
或 hg38。WGBS-derived array 与 EPIC→450K 是独立、需显式 opt-in 的路线。五个外置
runtime 仍为 `USER_SUPPLIED_SUPPORTED`；缺失或摘要不匹配会阻断，core 不会回退到
源码树，也不会自动下载。

## 输出、replay 与 retry

workflow exit、输出契约和科学证据是不同事实。MeDeCom 等匿名 component 不可直接
当作命名细胞类型评分。`replay-plan` 永远只展示；只有显式
`replay-execute --execute` 才在新目录重新验证身份并启动严格 replay，不执行历史
shell command、不使用 Nextflow resume、也不复用 partial output。跨版本 replay
尚未实现。`retry-failed-tool --execute` 是一个已终态失败/超时工具的单工具 fresh
restart，不是 replay、resume 或步骤级恢复。

当前 R/MeDeCom wrapper 会记录外部 stderr、阶段事件和元数据；若只有 `42 warnings`
汇总，则具体 warning 内容仍未知，不能推断其无害或已完整捕获。

## 排错、卸载与反馈

请查看 `--help`、plan/doctor 的 JSON、receipt、lease、`run_manifest.json`、
`result_summary.json` 和派生 report，不要修改历史记录。只提交脱敏日志和相对路径，
不要提交凭据、token、私人输入或未经批准的 reference/runtime 内容。

卸载 Python 包不会删除用户自有输入、asset home、runtime 或结果。删除前先检查共享
依赖和运行记录。引用 core 请使用 [`CITATION.cff`](../CITATION.cff)；外置资产和
上游算法继续遵循各自许可，core 不因此授予再分发许可。

core 作者顺序为 Mochen Zhang、Xiangyu Yu、Yaoke Wei；联系人为 Mochen Zhang
（`zhangmochen@big.ac.cn`）。三位作者声明的单位均为“中国科学院北京基因组研究所
（国家生物信息中心）”，英文采用 “Beijing Institute of Genomics, Chinese
Academy of Sciences (China National Center for Bioinformation)”。三位均未提供
ORCID。core 继续使用 MIT 许可证并允许商业使用。Reference material 与 core 分开：
Owner 已为适用许可人有权授权的 reference 自有贡献选择 CC BY-NC 4.0；符合该许可的
非商业研究使用、分享和改编无需另行向项目申请，但必须保留署名、许可证和修改说明。
24 个 archive 保留 GPL adapter notice；native-WGBS 对象采用逐对象分层 notice 与
保留的上游条款。明确标注的第三方材料继续遵守其适用许可。商业使用须事先取得有权授权的权利人另行
许可，可联系 Mochen Zhang（`zhangmochen@big.ac.cn`）请求转达；联系开发者不等于
已经获得许可，也不能替代上游权利人授权。大学或学术机构身份本身不自动决定某项使用
属于非商业用途。应以每个 catalog 记录和 object notice 为准；联系邮箱不等于自动
获得许可。
