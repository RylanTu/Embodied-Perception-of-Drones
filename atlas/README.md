<div align="center">

# Atlas 200i DK A2 一体化服务

双USB采集 · 滑台控制 · RPLIDAR全行程检测 · 压力模型推理

</div>

Atlas通过两条独立USB串口连接ESP32-S3与RPLIDAR，统一完成200 Hz压力接收、滑台控制、批量采集、点云同步、CSV保存、同条件绘图和有/无障碍推理。ESP32原生USB使用JSON行协议；服务也保留原二进制透明串口兼容模式并自动识别。电脑只需通过网线访问Atlas网页。

采集和在线推理使用ESP32上传的200 Hz压力；CSV中的`pressure_pa_1..5`已经经过SDP3x `0x3615`读取间平均和ESP32单向卡尔曼滤波。批次结束后生成SVG图像时仍可额外使用绘图滤波；网页可修改绘图中值窗口、EMA权重或关闭绘图滤波，修改后的参数从下一次生成图像起生效。训练与推理必须使用相同的ESP32采样与滤波配置。

## 启动

```bash
cd /home/HwHiAiUser/drone/atlas
python3 -m pip install --user -r requirements.txt
./run.sh --host 0.0.0.0 --port 8080
```

电脑通过有线网络访问`http://192.168.137.100:8080`。ESP32默认是`/dev/ttyACM0`，RPLIDAR通常是`/dev/ttyUSB0`；二者均可在网页修改。优先使用`/dev/serial/by-id/`固定路径：

```bash
python3 -m serial.tools.list_ports -v
ls -l /dev/serial/by-id/
```

网页显示`CRC错误计数: 0`表示没有CRC错误。

## 网页采集

“实时雷达地图”显示三维累积点云和当前扫描截面，开启雷达并收到ESP32位置后自动更新。
拖动旋转、滚轮缩放，“重置视角”恢复默认角度；“暂停显示”只暂停浏览器刷新，
“清空预览”只清除显示缓存，不删除实验CSV。预览最多保留最近100000点，每圈最多抽取2000点，颜色表示高度。
地图根据固定45°安装角与ESP32脉冲推算位置拼接，属于滑台点云可视化，并非SLAM。
更新时请同步部署完整atlas目录，重启服务并在浏览器按Ctrl+F5刷新。

缓启停按距离mm设置，测量默认225/75 mm，复位150/150 mm；等待参数仍为ms。需同时更新ESP32固件和完整`atlas/`目录，重启服务并刷新页面。旧的毫秒参数方案按原速度自动换算后载入，核对后重新保存；新CSV为schema 6，使用`accel_mm`、`decel_mm`并加入雷达与融合字段。第一次短程回10 mm不再要求网页先观察到“运动中”，而是等待命令确认后的新到位状态。

打开`http://Atlas有线IP:8080`后，可以在同一页面：

- 在“批量采集”顶部输入方案名称并点击“保存当前参数”，将当前实验条件、标签、次数、位置、速度、缓启停、实验节拍、软限位和脉冲参数保存为一套方案。
- 从下拉框选择方案并点击“载入”即可恢复参数；载入不会自动启动实验。同名保存会覆盖原方案，也可以单独删除。
- 实验参数方案保存在Atlas端的`experiment_presets.json`，服务重启后仍会保留。

- 查看五路实时压力、滑台位置和推理结果；
- 配置2 m软限位、80 pulse/mm、手动运动及软件停止；
- 分别设置测量/复位速度、缓启动、缓停止；
- 默认测量速度1500 mm/s，最高2000 mm/s（2 m/s）；80 pulse/mm时最高对应160 kpps；
- 自动执行1～500次独立实验，正向采集、反向复位；
- 启用ESP32模拟数据联调整条USB链路；
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
    "window_samples": 200,
    "input_channels": 3,
    "channels": [2, 3, 4],
    "probability_threshold": 0.885,
    "consecutive_hits": 3,
    "hold_ms": 300
  }
}
```

`window_samples`、`channels`和`probability_threshold`沿用电脑训练结果；当前部署将`consecutive_hits`设为3，即连续3次推理达到阈值才确认障碍并停止。`channels: [2, 3, 4]`表示按该顺序将2、3、4号传感器送入模型。OM加载使用CANN环境提供的`ais_bench.infer.interface.InferSession`；如果模型不存在、环境缺少`ais_bench`或推理失败，服务会返回规则后端并在状态中记录`model_error`，不会把失败伪装成NPU成功。

### 2、3、4号模型一键部署

将完整`atlas`目录上传到板端后执行：

```bash
cd /home/HwHiAiUser/drone/atlas
chmod +x deploy_ch234.sh run.sh
./deploy_ch234.sh
./run.sh --host 0.0.0.0 --port 8080
```

如果目标OM已经存在，脚本会直接复用并刷新运行配置；不存在时才调用ATC转换ONNX。需要强制重新转换时执行`FORCE_ATC=1 ./deploy_ch234.sh`。脚本会写入3通道顺序、阈值0.885、连续3次命中确认及障碍自动停止参数。网页“推理参数”中可以调整模型阈值，也可以单独关闭“障碍自动停止”。该功能只在自动批次的正向采集阶段生效：默认忽略缓启动距离以及其后100 mm，只显示数据、不参与障碍判断；越过起步豁免段后，连续3个推理点达到阈值才发送STOP。检测到障碍后当前批次立即结束，滑台保持停止锁定，不会自动清除停止、回到起点或继续下一次实验。如需再次运行，必须由操作者在页面手动清除停止并复位。模拟数据不会触发自动停止。

## 安全

Atlas持续发送心跳；服务退出或ESP32串口断开超过1秒时，ESP32会停止脉冲、关闭使能并进入软件停止锁定。滑台命令均等待ESP32确认，停止后还会等待`moving=false`再允许恢复。

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

### 点云持续保存与模型导出

雷达收到有效完整扫描圈并有ESP32位置数据后，自动连续保存，无需开始批量采集。
网页“实时雷达地图”下显示本次服务的保存点数；“点云文件”可下载当前及历史文件。
暂停显示、关闭浏览器、清空预览均不会停止后台保存或删除文件。关闭雷达或停止服务才停止接收新点云。

默认路径：`/home/HwHiAiUser/drone/atlas/data/lidar_archive/<会话>/`（使用`--data`时跟随该目录）。

- `points_000001.csv`、`points_000002.csv`……：达到10万点后从下一圈另开文件，不拆分完整圈、不覆盖旧文件。
- `metadata.json`：保存雷达配置、坐标单位、时间戳定义；服务重启或雷达配置改变后创建新会话。
- CSV逐点保存扫描编号、接收UTC时间、估计单调时间、滑台位置、角度、距离、质量及XYZ，保留数值精度，不使用网页预览抽稀。
- 网页最多显示100000个预览点（每次请求完成后间隔500ms刷新），磁盘文件不受该上限影响。不会自动删除历史点云，请定期备份并检查磁盘空间。

每圈写入后关闭文件，正常停止不残留应用缓冲。突然断电仍可能损失操作系统未落盘部分。
写盘失败时网页明确报错，持续记录锁定停止；排除磁盘故障后重启服务。失败文件可能有不完整尾行，不能视为完整实验。
已保存计数仅统计当前服务确认写入成功的圈，不代表历史总点数。

导出前先关闭雷达采集，或复制已经写完的会话到电脑，避免下载/转换正在增长的文件。
在`atlas`目录执行以下命令，将`实际会话目录名`替换为文件列表中的目录名：

```bash
python3 export_lidar_ply.py data/lidar_archive/实际会话目录名 cloud.ply
```

支持单个点云CSV或一个会话目录，生成包含XYZ（米）及质量字段的ASCII PLY；输出文件存在时拒绝覆盖。
脚本只用Python标准库，按流转换，但需要可容纳中间点列表的临时磁盘空间。PLY是点云模型，不是封闭三角网格。

保存范围仍受现有解析、距离、质量和有效圈判定限制；缺少位置或不合格扫描不进入此档案。
坐标位置来自ESP32输出脉冲累计与时间插值，不是编码器实测值。连续档案包含静止、测量和复位扫描，
不自动赋予障碍物标签；训练请继续配合批次压力CSV及`*_lidar.csv`的时间、试次和标签对齐，不能把同一次实验的点随机拆成训练集和测试集。

更新部署需同时复制`service.py`、`lidar_archive.py`、`export_lidar_ply.py`及整个`web/`目录，
重启Atlas服务后在浏览器按Ctrl+F5；ESP32固件无需改动。
