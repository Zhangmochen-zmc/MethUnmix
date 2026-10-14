# MethUnMix reference 安装指南——catalog 1.0.0

本指南适用于外置 candidate.6 catalog 与尚未发布的 MethUnMix 修复后 1.0.0
core 候选 `fixed-1.0.0-candidate.1-20261014`；它不同于现有直链中的旧 1.0.0
wheel。Core 与 reference 版本相互独立：升级 core 不会静默替换 reference，发布
reference 也不会改变科学状态。

## 权威 catalog 身份

```text
URL：https://download.cncb.ac.cn/methbank/MethUnMix/reference/1.0.0/catalog/methunmix-reference-1.0.0-owner-rights-public-safe-candidate.6/catalog.json
字节数：528233
SHA256：7ef6600563a0c182f14593ce1236b95558d0a860dfde22d282364b0e0cd805f2
selector：33 个唯一值，33 个 PUBLIC_DOWNLOADABLE
声明的 archive 总字节数：18461347873
```

每个 archive 使用
`.../objects/sha256/<archive-sha256>/<archive-name>.tar.gz` 内容寻址。不得只凭
文件名判断身份；selector、字节数、SHA256、许可字段和 notice 链接共同构成契约。

candidate.6 发布闭环匿名读取并核验了新建的 11 个小型 catalog/notice/evidence
对象。历史证据覆盖 33 个 archive 字节；本次闭环只重新通过安装版客户端
fetch/import/verify 一个代表 archive，并未重下全部 33 项。

## 1. 安装并识别客户端

安装前按候选证据清单核对 wheel SHA256：

```bash
python3 -m venv /absolute/path/to/methunmix-venv
/absolute/path/to/methunmix-venv/bin/python -m pip install \
  --no-deps /absolute/path/to/fixed-candidate/methunmix-1.0.0-py3-none-any.whl
/absolute/path/to/methunmix-venv/bin/methunmix --version
```

## 2. 显式审计 catalog

客户端不会自动发现在线 catalog。选择新的可写目录，并显式传入精确 URL：

```bash
METHUNMIX=/absolute/path/to/methunmix-venv/bin/methunmix
ASSET_HOME=/absolute/path/to/methunmix-assets
METADATA=/absolute/path/to/methunmix-catalog-metadata
CATALOG_URL=https://download.cncb.ac.cn/methbank/MethUnMix/reference/1.0.0/catalog/methunmix-reference-1.0.0-owner-rights-public-safe-candidate.6/catalog.json

"$METHUNMIX" --home "$ASSET_HOME" asset audit --online \
  --catalog-url "$CATALOG_URL" --metadata-dir "$METADATA"
"$METHUNMIX" --home "$ASSET_HOME" reference available \
  --metadata-dir "$METADATA"
```

`asset audit --online` 是显式网络步骤。应保留 metadata 与 receipt，并在 fetch 前
确认 catalog 字节数和 SHA256。

## 3. 先规划

下面的命令不会安装资产或运行模型：

```bash
"$METHUNMIX" --home "$ASSET_HOME" asset plan \
  --metadata-dir "$METADATA" --scenario brain --platform 450k \
  --input-type array --reference builtin.brain.450k@1.0.0 \
  --tools Houseman --accelerator cpu --mode decon --summary
```

reference 安装成功后，计划仍可能因外置 runtime/SIF/model/cache 缺失而阻断。
`PUBLIC_DOWNLOADABLE` 只描述 reference archive，不代表完整执行环境可用。

## 4. 获取、安装并校验一个精确 selector

```bash
"$METHUNMIX" --home "$ASSET_HOME" reference fetch --install \
  --metadata-dir "$METADATA" builtin.brain.450k@1.0.0
"$METHUNMIX" --home "$ASSET_HOME" reference validate \
  --checksums builtin.brain.450k@1.0.0
"$METHUNMIX" --home "$ASSET_HOME" asset verify \
  reference.builtin.brain.450k@1.0.0 --json
```

中断下载必须保持在 staging 状态并重新校验；不完整或摘要错误的对象不得成为已安装
资产。同一精确身份重复安装应幂等；不同版本属于显式 upgrade 决策，不能静默替换。

若 archive 已通过批准的离线方式取得，应使用普通本地安装路径，不能伪造 catalog URL：

```bash
"$METHUNMIX" --home "$ASSET_HOME" reference install \
  /absolute/path/to/exact-reference.tar.gz
```

## 5. 检查运行准备情况

安装路线所需全部精确依赖后，对相同选择运行 `plan` 和 `doctor`。Array 路线不传
`--genome-build`；native WGBS 路线必须声明精确 hg19/hg38 契约。缺失或漂移资产应
fail closed。Reference verify 成功不表示 workflow 或科学结论通过。

## 许可边界

MethUnMix core 使用 MIT。项目 reference 数据仅在适用许可人有权授权的范围内采用
CC BY-NC 4.0；24 个 archive 保留 GPL adapter notice；native-WGBS 对象采用逐对象
分层 notice 与保留的上游条款。下载或再分发前必须阅读精确 catalog 记录和 notice。
学术机构身份本身不自动等于非商业使用，联系邮箱也不等于已经获得许可。

## 证据边界

应分别记录 core 版本/content identity、catalog URL/digest、完整 selector、archive
大小/digest、安装 manifest digest、asset home、runtime/SIF 身份、参数及 receipt。
Reference 可下载不提升 READY/RU，不验证模型，也不证明生物学准确度。
