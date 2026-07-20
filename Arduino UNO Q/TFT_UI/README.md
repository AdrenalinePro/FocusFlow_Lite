# FocusFlow Lite v2.0 — TFT 显示模块

基于 Arduino UNO Q (STM32U585) + ILI9341V 2.8" TFT 彩屏的专注力监测 UI。

## 硬件

| 组件 | 型号 |
|---|---|
| 主控 | Arduino UNO Q (STM32U585, Cortex-M33, 3.3V I/O) |
| 屏幕 | CL028CK1001-18A 2.8" TFT (ILI9341V, 240×320, 无触摸) |
| 转接板 | 28005 SPI 适配板 |

### 接线（软件 SPI，10MHz）

| TFT 引脚 | UNO Q 引脚 | 说明 |
|---|---|---|
| VCC | 3.3V | 电源输入 |
| GND | JSPI pin 6 | 接地输入 |
| CS | D10 | 片选 |
| RESET | D8 | **独立 GPIO**（不能用 JSPI pin 5 NRST） |
| DC | D9 | 数据/命令 |
| MOSI | D11 | 主出从入 |
| SCK | D13 | 时钟 |
| LED | D7 | 背光 PWM |
| MISO | JSPI pin 1 | 未使用，仅占位 |

## 项目结构

```
uno-q_tft_ui/
├── focusflow_ui.h              # UI 类声明、布局常量、颜色定义
├── focusflow_ui.cpp            # UI 绘制实现
├── focus_chinese_font.h        # 16×16 点阵中文字库 (69 字, 2.2KB)
├── break_image.h               # 休息界面全屏图片 (240×320 RGB565, 602KB)
├── 永雏塔菲.jpg                 # 休息界面原始图片
├── focusflow_demo.ino          # 主程序 (JSON 解析 + demo 循环)
├── 可显示汉字列表.txt           # 字库覆盖的 69 个汉字一览
├── DEMAND.md                   # 需求文档
└── README.md
```

## 编译

1. Arduino IDE / App Lab 中开发板选 **UNO Q (MCU/STM32U585)**
2. 安装库：`Adafruit GFX Library`、`Adafruit ILI9341`、`Adafruit BusIO`
3. 打开 `focusflow_demo.ino`，编译上传

## JSON 协议

Linux 端通过 USB 串口（115200 bps）发送单行 JSON 控制显示。所有字段除 `cmd` 外均可选。

### 专注界面

```json
{"cmd":"focus","pct":82,"elapsed":1122,"total":1500,"screen":"VS Code","status":"高度专注"}
```

| 字段 | 类型 | 说明 | 缺省值 |
|---|---|---|---|
| `pct` | int 0-100 | 专注度百分比 | 82 |
| `elapsed` | int (秒) | 已学习时长 | 1122 |
| `total` | int (秒) | 计划总时长 | 1500 |
| `screen` | string | 当前窗口/应用名 | `"VS Code"` |
| `status` | string | 专注等级描述 | `"高度专注"` |

### 走神告警

```json
{"cmd":"alert","screen":"B站"}
```

| 字段 | 类型 | 说明 | 缺省值 |
|---|---|---|---|
| `screen` | string | 分心应用名 | `"B站"` |

### 休息界面

```json
{"cmd":"break","remain":154,"next":1500}
```

| 字段 | 类型 | 说明 | 缺省值 |
|---|---|---|---|
| `remain` | int (秒) | 休息剩余时长 | 154 |
| `next` | int (秒) | 下一段学习时长 | 1500 |

> **注意**：休息界面已改为全屏显示 `永雏塔菲.jpg`（240×320），`remain` 和 `next` 参数暂不显示在屏幕上，保留仅为协议兼容。若要还原文字 UI，将 `showBreakScreen` 替换为 prior 版本即可。

### 其他

```json
{"cmd":"ping"}     → 返回 {"status":"pong"}
```

## 集成到主项目

### 文件角色

| 文件 | 角色 | 集成时 |
|---|---|---|
| `focusflow_ui.h` | UI 类声明 | **不动，直接复制** |
| `focusflow_ui.cpp` | UI 绘制实现 | **不动，直接复制** |
| `focus_chinese_font.h` | 点阵字库 | **不动，直接复制** |
| `focusflow_demo.ino` | 独立演示 + JSON 解析示例 | **不复制**——仅作参考 |

核心三文件（`.h`、`.cpp`、`_font.h`）是纯驱动层，不依赖 demo sketch 的任何逻辑。`focusflow_demo.ino` 只是驱动层的测试外套，其 `handleCommand()` 函数展示 JSON 字段到 UI 方法的映射关系。

### 集成步骤

**1. 复制核心文件**

将以下三个文件复制到主项目目录：
```
focusflow_ui.h
focusflow_ui.cpp
focus_chinese_font.h
```

**2. 在主 sketch 中声明并初始化**

```cpp
#include "focusflow_ui.h"

// 引脚定义（与接线一致）
#define PIN_CS    10
#define PIN_DC     9
#define PIN_MOSI  11
#define PIN_SCK   13
#define PIN_RST    8
#define PIN_LED    7
#define PIN_MISO  12

FocusFlowUI ui(PIN_CS, PIN_DC, PIN_MOSI, PIN_SCK, PIN_RST, PIN_LED, PIN_MISO);

void setup() {
    // ... 其他初始化 ...
    ui.begin();
}

void loop() {
    // ... 读取串口 / RPC 回调 ...
}
```

**3. 在 RPC 回调中分发命令**

收到 JSON 后，根据 `cmd` 字段调用对应 UI 方法。以下代码可直接嵌入主项目的 `handleCommand()` 或 RPC 回调函数（核心三文件不需要添加这些——这些写在主项目的 sketch 里）：

```cpp
void handleCommand(const char* json) {
    const char* cmd = jsonGetStr(json, "cmd");   // 见 demo sketch 中的实现
    if (!cmd) return;

    if (strcmp(cmd, "focus") == 0) {
        uint8_t  pct     = (uint8_t) jsonGetLong(json, "pct", 0);
        uint32_t elapsed = (uint32_t) jsonGetLong(json, "elapsed", 0);
        uint32_t total   = (uint32_t) jsonGetLong(json, "total", 0);
        const char* scr  = jsonGetStr(json, "screen");
        const char* st   = jsonGetStr(json, "status");
        ui.showFocusScreen(pct, elapsed, total,
                           scr ? scr : "",
                           st  ? st  : "高度专注");

    } else if (strcmp(cmd, "alert") == 0) {
        const char* scr = jsonGetStr(json, "screen");
        ui.showAlertScreen(scr ? scr : "");

    } else if (strcmp(cmd, "break") == 0) {
        uint32_t remain = (uint32_t) jsonGetLong(json, "remain", 0);
        uint32_t next   = (uint32_t) jsonGetLong(json, "next", 0);
        ui.showBreakScreen(remain, next);
    }
}
```

`jsonGetStr` / `jsonGetLong` 的实现从 `focusflow_demo.ino` 中复制即可（零依赖，纯 `strstr` + `atol`）。

### 数据流

```
Linux ──[JSON/UART]──→ main.ino
                         │
                    rpc_slave 解析
                         │
                    handleCommand(json)
                         │
                    ┌────┼────┬──────────────┐
                    ▼    ▼    ▼              ▼
              ui.showFocusScreen(...)       ui.showAlertScreen(...)
              ui.showBreakScreen(...)
                         │
                    FocusFlowUI (focusflow_ui.cpp)
                         │
                    ILI9341 TFT
```

## 中文字库

自研 16×16 点阵字库，69 个汉字，共 2208 字节。覆盖：

- **UI 固定文本**：专、注、度、状、态、高、学、习、中、屏、幕、工、作、摸、鱼、提、醒、走、神、请、回、到、振、动、已、启、休、息、时、间、剩、余、下、一、段、好、吧、正、常、监、测、勿、分、心
- **常见应用名**：站、微、信、抖、音、博、知、淘、宝、游、戏、视、频、乐、网、页、小、红、书、文、档、编、辑、终、端

未知汉字在屏幕上显示为黄色 `?`，不会崩溃或乱码。ASCII 字符（如 `VS Code`、`YouTube`）通过 Adafruit_GFX 内置字体渲染，无需字库支持。

## 布局

竖屏 240×320，三界面共享统一区域划分，切换时无元素偏移：

```
┌──────────────────────┐ y=0
│ 标题栏 (40px, 彩色)   │
├──────────────────────┤ y=52
│                      │
│ 卡片 ×4              │ 专注/休息: 4 张卡片
│ (或图标 + 卡片)      │ 告警: 警告图标 + 3 张卡片
│                      │
├──────────────────────┤ y=304
│ 底部状态栏 (16px)     │
│ (留黑)               │ y=320
└──────────────────────┘
```

## 行为

- 上电后显示专注界面（缺省参数）
- 收到 JSON 命令立即切换对应界面
- 30 秒无串口输入后恢复 demo 自动循环（每 5 秒切换一个界面）
- 背光 PWM 默认 80% 亮度

## 平台注意事项

- **Serial** 在 STM32U585 上是 `BridgeMonitor<>`，不支持 `printf()`。代码中所有格式化均使用 `snprintf` 到 buffer 再 `print`
- **硬件 SPI 不可用**：D11/D13 走 SPI2，Arduino 默认 `SPI` 对象走 SPI1，因此必须用 6 参数构造器走软件 SPI
- **RST 接独立 GPIO**：ICSP pin 5 (NRST) 在 USB 下载时无复位脉冲，TFT 控制器永不被硬件复位导致白屏
