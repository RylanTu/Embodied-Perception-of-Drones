# ESP32-S3 五路压差采集与 P100S 伺服控制器

ESP-IDF 5.3+ 工程。通过 TCA9548A 的通道 0～4 读取五个 SDP3x，使用 ESP32-S3 原生 USB Serial/JTAG 向电脑发送数据，并支持 P100S 的脉冲/方向或 RS485 Modbus RTU 控制。

## 默认接线

| 功能 | ESP32-S3 GPIO | 外部连接 |
|---|---:|---|
| I2C SDA / SCL | 8 / 9 | TCA9548A SDA / SCL，需 3.3V 上拉 |
| 脉冲 / 方向 / 使能 | 4 / 5 / 6 | 必须经过适合 P100S CN1 的 5V 光耦驱动电路 |
| RS485 TX / RX / DE | 17 / 18 / 16 | 连接 3.3V RS485 收发器，再接 CN3/CN4 |
| USB | GPIO 19 / 20 | 板载原生 USB，禁止占用这两个引脚 |

SDP3x 地址默认 `0x21`，TCA9548A 默认 `0x70`。五个传感器分别接 TCA 通道 0～4。ESP32 GPIO **不能直接连接** P100S 的 5V 脉冲端子，也不能直接连接 RS485 A/B。

## 配置和编译

```text
idf.py set-target esp32s3
idf.py menuconfig
idf.py build flash
```

配置位于 `Drone controller` 菜单，可选择：单传感器直连调试模式、采样频率（默认 100 Hz）、CSV/JSON/二进制输出、脉冲或 Modbus 模式、全部 GPIO、脉冲频率范围和 RS485 参数。N16R8 的 Flash/PSRAM 默认值位于 `sdkconfig.defaults`。

### 单传感器直连调试模式

在 `idf.py menuconfig -> Drone controller` 中启用 `Direct single-sensor debug mode (bypass TCA9548A)`。启用后，将一个 SDP3x 直接连接到 GPIO8（SDA）和 GPIO9（SCL）；固件完全不初始化或访问 TCA9548A。直连传感器作为第 1 路输出，其余四路标记为无效。伺服控制仍按所选的脉冲或 Modbus 模式正常初始化，可继续使用网页或串口命令控制。由于 SDP3x 默认地址相同，一次只能直连一个传感器。

当直连调试模式和 `Pulse + direction` 模式同时启用时，上电后 GPIO4 会自动输出连续的 50% 占空比测试方波。默认频率为 `Default pulse/test-wave frequency` 配置值（10 kHz）。执行 `MOVE` 或 `STOP` 会终止连续测试方波；重新上电后恢复。

P100S 面板参数应与配置一致：从站地址 PA71 默认 1，波特率 PA72 手册出厂值对应 9600，协议 PA73 默认 8-N-2，固件默认值与此一致。若修改 PA73，请同步修改 `DRONE_RS485_PARITY/STOP_BITS`（ESP-IDF 枚举值）。手册没有提供伺服运行控制寄存器表，因此固件只实现可靠的通用 `0x03`/`0x06` 操作，未猜测运行寄存器。

## USB 串口命令

```text
ENABLE 1
ENABLE 0
MOVE <脉冲数> <方向0/1> <频率Hz> <加速ms> <减速ms>
STOP
CLEAR
STATUS
FORMAT CSV|JSON|BINARY
MBREAD <寄存器> <数量1..16>
MBWRITE <寄存器> <数值>
```

`STOP` 是软件急停，会立即停止后续脉冲并关闭使能；工业设备仍必须安装独立硬件急停回路。RMT 分块最多可能在收到急停后额外完成当前 64 个脉冲。

## 测试网页

网页位于 `web/index.html`。Web Serial 需要 Chrome/Edge 和安全上下文，建议在工程目录运行：

```text
python -m http.server 8000 -d web
```

然后打开 `http://localhost:8000`，点击“连接串口”。网页会自动选择 ESP32-S3 USB 串口并切换到 JSON 输出，显示五路实时数据和控制面板。

网页连接时会关闭 DTR/RTS，避免带 USB 转串口芯片的开发板因控制线状态持续复位。请选择连接 ESP32-S3 GPIO19/20 原生 USB 的端口；若开发板同时提供 `UART` 和 `USB/OTG` 两个接口，应使用后者。直连传感器断开或接线错误时，固件不会重启，而会输出无效状态并每秒自动重试。

## 数据格式

- CSV：`DATA,毫秒,p1,t1,ok1,...,p5,t5,ok5`
- JSON：每行一个对象，包含 `ms` 和五个 `{p,t,ok}`
- 二进制：小端、packed；魔数 `0xA55A`，版本 1，压力单位 mPa，温度单位 0.01°C，末尾 Modbus CRC16
