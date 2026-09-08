# Atlas 200I DK A2 一体化实验与推理服务

Atlas通过无线透明串口与ESP32双向通信，统一完成200 Hz压力接收、滑台控制、批量采集、CSV保存、同条件绘图和有/无障碍推理。ESP32端的SDP3x使用最早固件的`0x3615`连续测量方式，由传感器在相邻读取之间内部平均，随后应用单向卡尔曼滤波`Q=0.03 Pa²、R=0.02 Pa²`；温度与慢状态以10 Hz更新。电脑只需用网线访问Atlas网页，不再连接ESP32 USB。

采集和在线推理使用ESP32上传的200 Hz压力；CSV中的`pressure_pa_1..5`已经经过SDP3x `0x3615`读取间平均和ESP32单向卡尔曼滤波。批次结束后生成SVG图像时仍可额外使用绘图滤波；网页可修改绘图中值窗口、EMA权重或关闭绘图滤波，修改后的参数从下一次生成图像起生效。训练与推理必须使用相同的ESP32采样与滤波配置。

## 启动

```bash
cd /home/HwHiAiUser/drone/atlas
python3 -m pip install --user -r requirements.txt
./run.sh --host 0.0.0.0 --port 8080
```

电脑通过有线网络访问`http://192.168.137.100:8080`。默认串口是`/dev/ttyACM0`、115200 baud，可以在网页修改。实际设备也可能是`/dev/ttyUSB0`，使用以下命令确认：

```bash
python3 -m serial.tools.list_ports -v
```

网页显示`CRC错误计数: 0`表示没有CRC错误。

## 网页采集

缓启停现在按距离mm设置，测量默认225/75 mm，复位150/150 mm；等待参数仍为ms。需同时更新ESP32固件、`service.py`、`protocol.py`和`web/`，重启服务并刷新页面。旧的毫秒参数方案按原速度自动换算后载入，核对后重新保存；新CSV版本5使用`accel_mm`、`decel_mm`列。第一次短程回10 mm不再要求网页先观察到“运动中”，而是等待命令确认后的新到位状态。

打开`http://Atlas有线IP:8080`后，可以在同一页面：

- 在“批量采集”顶部输入方案名称并点击“保存当前参数”，将当前实验条件、标签、次数、位置、速度、缓启停、实验节拍、软限位和脉冲参数保存为一套方案。
- 从下拉框选择方案并点击“载入”即可恢复参数；载入不会自动启动实验。同名保存会覆盖原方案，也可以单独删除。
- 实验参数方案保存在Atlas端的`experiment_presets.json`，服务重启后仍会保留。

- 查看五路实时压力、滑台位置和推理结果；
- 配置2 m软限位、80 pulse/mm、手动运动及软件停止；
- 分别设置测量/复位速度、缓启动、缓停止；
- 默认测量速度1500 mm/s，最高2000 mm/s（2 m/s）；80 pulse/mm时最高对应160 kpps；
- 自动执行1～500次独立实验，正向采集、反向复位；
- 启用ESP32模拟数据联调整条无线链路；
- 下载Atlas本地保存的CSV和同条件SVG对比图。

原始文件默认保存到：

```text
/home/HwHiAiUser/drone/atlas/data/<实验条件>/
```

每批生成一个CSV和同名SVG批次图；同条件目录的`condition_comparison.svg`会在批次结束或停止后自动更新。网页“自动生成图像”区域直接预览这两张图，也可以从文件列表下载。绘图模块完全使用Python标准库，不依赖Matplotlib。浏览器关闭或刷新不会中断正在运行的批次，因为状态机运行在Atlas服务端。一个批次内必须保持相同标签，有障碍和无障碍要分别采集。

历史CSV也可以使用独立脚本生成单通道滤波叠加图，不会修改原始数据：

```bash
python3 generate_filtered_plot.py data/某个条件 --channel 1 \
  --kalman-q 0.03 --kalman-r 0.02 --show-raw
```

输入可以是单个CSV或目录。单文件默认输出`原文件名_filtered.svg`，目录默认输出
`filtered_comparison.svg`；蓝色实线表示无障碍，红色虚线表示有障碍。脚本使用
前向/反向一维卡尔曼结果的均值减少时间滞后；增大`Q`会更贴近原始变化，增大`R`
会获得更强的平滑效果。

## 推理后端

未部署训练模型时，服务使用现有LIF/多通道规则作为联调回退。若Python环境存在`whisker_npu`且配置满足要求，可以继续使用现有Ascend C LIF内核。

电脑训练完成后，将模型转换成OM并复制到：

```text
/home/HwHiAiUser/drone/atlas/models/pressure_ramp.om
```

在`config.json`中启用：

```json
{
  "model": {
    "enabled": true,
    "path": "models/pressure_ramp.om",
    "device_id": 0,
    "window_samples": 100,
    "probability_threshold": 0.5,
    "hold_ms": 300
  }
}
```

`window_samples`和`probability_threshold`必须与电脑训练生成的`manifest.json`一致。OM加载使用CANN环境提供的`ais_bench.infer.interface.InferSession`；如果模型不存在、环境缺少`ais_bench`或推理失败，服务会返回规则后端并在状态中记录`model_error`，不会把失败伪装成NPU成功。

## 安全

Atlas持续发送心跳；服务退出或无线链路断开超过1秒时，ESP32会停止脉冲、关闭使能并进入软件停止锁定。滑台命令均等待ESP32 ACK，停止后还会等待`moving=false`再允许恢复。系统仍没有物理急停、限位开关和绝对原点，软件保护不能替代机械安全装置。

## 测试

```bash
cd /home/HwHiAiUser/drone/atlas
PYTHONPATH=. python3 -m unittest discover -s tests -v
```

## 45° RPLIDAR采样、全行程检测与融合

用户提供的`45度程序.txt`是RPLIDAR扫描与固定45°坐标变换程序，不包含陀螺仪读数。工程已将其中与绘图无关的部分拆到`lidar_workflow.py`，状态流为：

```text
idle → warming（丢弃预热圈）→ sampling（只收完整合格圈）
     → deciding（冻结采样快照）→ complete
     ↘ stopped / fault（断流或总超时）
```

进入采样必须同时满足：字节流已锁定到合法5字节节点、收到完整圈标志、单圈有效点数达到阈值、角度覆盖达到阈值，并完成所有预热圈。进入决策必须完成配置的采样圈数；决策读取冻结快照，后续串口数据不会改变正在判断的样本。串口有数据但始终组不成合格圈时由总会话超时退出，不会无限等待。

集成服务现在使用两条互不抢占的USB串口：ESP32由`SerialController`负责，雷达由
`LidarController`持续扫描。建议使用`/dev/serial/by-id/...`固定设备身份，避免重启后
`ttyACM0`和`ttyUSB0`编号变化：

```bash
python3 -m serial.tools.list_ports -v
ls -l /dev/serial/by-id/
```

网页“设备连接”中填写雷达端口、波特率和安装俯角，选择“开启”后保存。雷达线程沿用
参考程序的`A5 25`停止、`A5 20`启动、标准5字节节点解析和45°坐标变换。区别是滑台
位置来自ESP32实时遥测并按单调时钟线性插值，不再使用“采集时间×设定速度”。

每个完整扫描圈先检查点数和角度覆盖，再在整个`0~2000 mm`行程走廊内聚类。候选点簇
必须连续出现多圈；没有把决策区间写死在滑台后段。默认几何范围位于`config.json`的
`lidar`段，可按安装位置修改：

```json
{
  "lidar": {
    "route_min_mm": 0,
    "route_max_mm": 2000,
    "corridor_half_width_mm": 350,
    "height_min_mm": -100,
    "height_max_mm": 1800,
    "cluster_bin_mm": 50,
    "cluster_min_points": 3,
    "persistence_revolutions": 3
  }
}
```

雷达结果只是候选，最终结果在时间与位置上和压力异常匹配。网页会显示四种状态：
`confirmed`（确认障碍）、`lidar_suspected`（雷达疑似）、`pressure_suspected`（压力疑似）
和`clear/unknown`。匹配容差位于`fusion`配置段，默认时间1500 ms、位置450 mm。

批次运行时，原压力CSV会增加雷达候选和融合字段；雷达开启时还会在同一目录生成
`*_lidar.csv`，逐点保存雷达时间戳、ESP32插值位置、角度、距离、质量和XYZ坐标，供后续
训练使用。

独立采样工具仍可用于安装标定。这会保存完整点云CSV和同名状态JSON；未给ROI时结果为
`undetermined`：

```bash
python3 lidar_capture.py --port /dev/ttyUSB1 --pitch-deg 45 \
  --roi X_MIN X_MAX Y_ABS Z_MIN Z_MAX \
  --minimum-roi-hits 3 --minimum-roi-revolutions 3 \
  --minimum-roi-hit-ratio 0.01
```

ROI坐标单位为米，坐标约定沿用参考程序：X为滑台方向、Y为左右、Z为高度。示例中的大写值是待标定占位符，不能原样执行。`--rail-speed-m-s`只用于独立工具复现参考程序；集成服务始终使用ESP32位置。

如果以后接入真正的IMU，可把实测俯仰角送入`StableAngleGate`：角度连续保持在45°±容差一段时间后仅触发一次`workflow.start()`；离开容差、数据非有限值或未达到保持时间都不会进入采样。当前参考文件没有IMU数据，所以命令行入口采用人工启动。

融合模块当前只输出检测结果和训练数据，不会因为未标定的雷达候选自动停止或控制滑台。
