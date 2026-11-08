# local-tools

此目录**不属于上游项目**，是本地为验证运行环境、补足缺口而添加的工具。

| 文件 | 说明 |
| --- | --- |
| `detach.py` | **后台守护化启动器**。「双重 fork + setsid」把进程放进独立会话，关掉终端 / 智能体回合一结束都不会被连带回收 |
| `ui.py` | **本地 Web 界面 + 同源代理**。上游服务没有网页界面（`GET /` 返回 404）且不发 CORS 头，所以浏览器打不开、另写页面也会被同源策略拦掉。这个脚本在 `/` 提供界面、在 `/api/*` 转发给 8765，两个问题一起解决 |
| `make_test_sample.py` | 用纯标准库造一个带 EXIF / XMP / JUMBF(C2PA) / COM / AI 生成器标识的 JPEG 测试样本 |
| `../run.sh` | 启动服务（默认同时起 API 和界面） |
| `../stop.sh` | 停止服务 |

## 启动 / 停止

```bash
./run.sh          # 同时启动 API(8765) + 网页界面(8780)
./run.sh api      # 只起 API
./run.sh ui       # 只起界面
./stop.sh         # 全部停止
./stop.sh api|ui  # 只停一个
```

浏览器打开 **http://127.0.0.1:8780**。

可用环境变量：`PYTHON`、`WATERMARKS_HOST`、`WATERMARKS_PORT`、`UI_HOST`、`UI_PORT`。

### 为什么不用 `nohup ... &`

从智能体沙箱里用 `nohup cmd & disown` 启动的进程，会在该条命令结束时随进程组一起被回收，
服务活不过一次工具调用。`detach.py` 用双重 fork + `setsid()` 建立独立会话，
彻底避开这个问题——`run.sh` 内部就是调它。

手动用法：

```bash
python3 local-tools/detach.py --pid logs/x.pid --log logs/x.log -- \
    python3 some_server.py --port 1234
```

## 造样本并自测

```bash
python3 local-tools/make_test_sample.py     # 输出 /tmp/wr_test.jpg
python3 service/scripts/inspect_image.py /tmp/wr_test.jpg
python3 service/scripts/clean_image.py /tmp/wr_test.jpg -o /tmp/wr_test.cleaned.jpg
python3 service/scripts/inspect_image.py /tmp/wr_test.cleaned.jpg   # 应报 C2PA: False
```

## 注意

`keep_non_ai_metadata: true` 会保住 ICC 色彩配置，但**实测也会放过未被识别的 APP1 段**（例如厂商自带的生成器标识），且清理后复检仍报 `AI metadata: False`（假阴性）。
目标是彻底清除 AI 痕迹时，用默认的全剥离行为，**不要**勾选它。

---

# 已整合：像素工作台（/studio）

来源：`/Users/tao/xiaohongshu/像素工作台.html`（282 KB 单文件，纯浏览器端，无网络请求）。
已复制为 `local-tools/pixel-studio.html`，并由 `ui.py` 在 **`/studio`** 路由提供，与清理页共用 8780 端口。

## 两个页面是同一根轴的两个方向

| 页面 | 路由 | 方向 |
| --- | --- | --- |
| 溯源清理 | `/` | **剥离** C2PA / EXIF / XMP 等 AI 溯源标记（Python，纯标准库） |
| 像素工作台 | `/studio` | **重写**相机参数元数据 + 可选像素变体（浏览器 Canvas + JS） |

## 工作台的能力（6 个模块）

| 模块 | 职责 |
| --- | --- |
| `data.js` | 机型档案表（Apple/HUAWEI/Xiaomi…按厂商分族）+ 国内地点 GPS 预设 |
| `metadata.js` | JPEG/PNG 元数据剥除 + EXIF/GPS/XMP 写入引擎；14 条 AI 生成器标记规则扫描 |
| `pixels.js` | 9 项像素变体（颗粒/空间变换/色彩/翻转/锐化/二次编码/RGB 位移/中值/低频扰动）+ 尺寸档位 + 垫图叠加 |
| `analyze.js` | 8 个特征族（spectral/phase/lsb/pixel/spatial/wavelet/dct/angular）+ 阈值标定 + 前后对比报告 |
| `zip.js` | 极简 ZIP 打包（store 模式） |
| `app.js` | 界面装配 + 处理管线 |

两种处理模式：
- **重编码**：解码 → 变体 → 按质量重新编码为 JPG（变体生效）
- **保留原像素**：不解码，直接在原 JPEG 字节上剥旧段写新段，画质零损失，仅支持 JPEG

导出：ZIP 内含全部 JPG + `元数据对比报告.json` + `元数据对比报告.csv` + `README.txt`。

## 整合方式：不是跳转，是同一张卡片里接着处理

在 `/` 的每张照片卡片上，检测完成后会多出一个 **「像素工作台处理」** 按钮。
点开即在卡片内展开面板，直接对**这一张照片**操作：

1. 引擎来源：`/studio` 被装进**同源隐藏 iframe**，页面直接取它的
   `PS_DATA / PS_META / PS_PIXELS / PS_ANALYZE` 来调用。
   **没有复制任何一行处理逻辑**，工作台升级这里自动跟着升。
2. 数据源：默认用原始文件；点过「清理并下载」之后自动换成**清理后的输出**，
   于是「先清 AI 溯源 → 再写相机参数」是一条流水线。
3. 面板可调：处理模式（保留原像素 / 重编码）、机型（57 个档案）、
   地点 GPS（55 个预设）、导出尺寸、9 项像素变体开关 + 强度。
4. 执行后：下载 `<原名>-studio.jpg`，并在面板里显示
   处理前后的 AI 标记扫描对比 + 容器特征对比 + 体积变化，结果成为下一轮的输入。

调用序列与工作台 app.js 的 `processOne()` 保持一致：
`scanBinary → stripJpeg / (decode → applyVariants → targetSize → encodeJpeg) → buildExifDict → writeMetadata → scanBinary`。

### 验证

用 Node 直接加载工作台的引擎库（UMD，无 DOM 依赖）跑通了整条管线：
扫描命中 `Midjourney 标记` + `[c2pa, jumb, XMP]`；处理后复扫 `hits=[] boxes=[]`；
回读 EXIF 得到 `Apple / iPhone 17 Pro Max / f2 / ISO125 / 6.762mm`，GPS 写入正确。
`applyVariants` 与 `targetSize` 的签名也已验证。

> 浏览器端的 canvas 编解码与 iframe 同源访问未做浏览器实测（本机无法安装
> agent-browser 的 Chromium），属标准用法，第一次点击请留意"正在加载引擎"是否正常消失。

## 附：独立入口

- `ui.py` 另有 `/studio` 路由可直接打开完整工作台（功能更全：批量、垫图、ZIP 打包）
- 清理页顶部有标签导航；工作台页注入了浮动「← 返回溯源清理」——**注入而非改文件**

## 合规提醒

工作台写入的机型、镜头、GPS、曝光时间都是模板生成的测试值，**不构成真实拍摄来源证明**。
重编码与像素变体可能让原有 C2PA 凭证失效、影响不可见标记的可检测性。
小红书对 AI 生成内容有标注要求，请按平台规则如实标注。
