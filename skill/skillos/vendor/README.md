# vendor/ —— 可选运行时附件（兜底层）

**这个目录默认是空的，而且大多数用户永远不需要它。**

启动器遵循「**检测优先**」：本机已有且在版本范围内的 Python / git / Node，直接用系统的。
只有检测不到、又不想让用户自己去装时，`vendor/` 里的便携运行时才会被使用。

## 目录规约

```
vendor/
├── manifest.json          # 锁定表：每个运行时的版本 + 下载地址 + sha256
├── win-x64/
│   ├── uv.exe             # uv 单文件（无系统 Python 时用它自举 CPython）
│   ├── MinGit/            # Windows 专用 git 便携版
│   └── python/            # 便携 CPython：发行包内由 build/build.py 注入；离线预置也放这里
├── mac-arm64/
│   └── uv
└── linux-x64/
    └── uv
```

> 在**对外发行包**里，`vendor/<平台>/python/` 不再是"可选" —— 它就是那份便携 CPython，
> 与包内的 `runtime/skillos/`（编译产物）一同保证用户「零安装」。
> 仓库里的这份为空，体积大、随平台而异，由 `build/build.py` 在装配时注入。

平台目录名由 `scripts/_common.py:platform_key()` 决定：`win-x64` / `mac-arm64` / `mac-x64` / `linux-x64` / `linux-arm64`。

## 两条获取路径

1. **在有网机器上抓**：`python scripts/vendor_fetch.py --platform <平台>`（先 `--list` 看清单，`--yes` 才真下）。
2. **手工放入**：把二进制按上面的路径摆好即可，脚本只做检测不做强校验。放完用 `python scripts/detect.py` 确认已被识别。

## 离线机器怎么用

在能联网的机器上跑 `vendor_fetch.py`，把产出的 `vendor/<平台>/` 整个目录拷到离线机器同位置。
之后启动器检测不到系统 Python 时会自动落到 `vendor/<平台>/`，**全程不需要再联网**。

## 安全约定

- `sha256` 为空 = 未锁定 → `vendor_fetch.py` 默认**拒绝安装**（除非显式 `--allow-unverified`）。
- 校验失败一律中止，不覆盖已有文件。
- 这个目录**不进 git**（见包根 `.gitignore` 说明）；体积大、且随平台而异，应由发行流水线生成。
