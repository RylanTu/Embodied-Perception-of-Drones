# 电脑端训练

训练输入是ESP32采集网页保存的批量CSV，当前模型只使用单通道`pressure_pa_1`。一份CSV可包含数十或数百个`trial_id`；脚本先把每次正向测量拆成独立实验。它自动寻找每次实验末端的共同快速下降，只使用下降之前的因果窗口；有障碍实验中紧邻下降前的向上斜坡为正窗口，更早的平台窗口及无障碍实验窗口为负窗口。`phase`不是`measure`的行和ESP32模拟数据都会被排除。

## 安装与运行

```powershell
cd training
python -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
python train.py E:\Research\2026-8-22-pressure --output output
```

当前目录命名规则为：`*_t.csv`用于训练，`*_v.csv`用于验证，`*_test.csv`用于最终测试。若没有这些后缀，脚本才按整份批次CSV自动拆分，此时每个标签至少需要3份独立CSV。正式训练建议累计300～500次完整实验，并分散到多个日期和独立批次。只有训练和验证文件时可以生成模型，但不能报告可信的最终测试准确率。

推荐采集顺序：

1. 先用模拟数据和5～10 mm低速运动验证通信、方向、缓启停和CSV写入。
2. 固定一个条件，分别采集无障碍批次和有障碍批次；一个批次中不能改变标签。
3. 每批20～50次，跨日期重复，并适当改变速度、障碍距离、安装误差和环境。
4. 保留从未参与调参的整批数据作为最终测试集；以整次实验的误报、召回和提前报警时间验收。

输出包括：

- `pressure_ramp.pt`：电脑端复现模型。
- `pressure_ramp.onnx`：用于ATC转换。
- `manifest.json`：数据划分、固定输入长度、概率阈值和测试指标。

已有`pressure_ramp.pt`时无需重新训练，可重新导出固定batch=1、适配Atlas
ATC的单文件ONNX：

```powershell
python training\export_onnx.py training\output\pressure_ramp.pt `
  --output training\output\pressure_ramp.onnx
```

默认输入为200 Hz下最近1秒的第1路原始压力。网络内部自行完成窗口首值扣除、一阶差分、训练集归一化和轻量卷积，因此ONNX只需要`[1,1,1,200]`原始压力输入。

## 转换到Atlas

在与板端CANN匹配的ATC环境中执行，并按实际芯片支持列表确认`soc_version`：

```bash
atc --model=output/pressure_ramp.onnx \
    --framework=5 \
    --output=output/pressure_ramp \
    --input_shape="pressure:1,1,1,200" \
    --soc_version=Ascend310B4
```

转换后得到`pressure_ramp.om`。最终验收必须按“整次扫描误报数、障碍召回率、急速下降前提前量”评估，不能只看窗口准确率。
