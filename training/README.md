# 电脑端训练

## 实时因果模型（当前推荐）

`train_causal_tcn.py`按真实上线方式逐点回放：每次推理只输入当前及此前的200个采样点，不使用未来数据；训练和评估都排除配置的减速距离以及终点急降。数据按完整批次划分，整次实验采用一个标签，通过多实例学习自动寻找有辨识度的局部片段，不把行程后段硬编码为决策区。验证集负责选择阈值，测试集只在最后评估一次。

```powershell
$env:PRESSURE_SKIP_NUMPY_BLAS_PROBE='1'  # 仅当前Conda出现MSMPI/NumPy DLL冲突时需要
python training\train_causal_tcn.py `
  "C:\Users\Rylan\Downloads\新建文件夹 (2)\1111\1111" `
  --output training\output_20260912_causal_tcn `
  --split-manifest training\output_20260912_multichannel\manifest.json
```

输出的`pressure_causal_tcn.onnx`固定输入为`[1,5,1,200]`、输出为`[1,1,1,1]`。部署时除了复制模型阈值，还必须使用`manifest.json`中的`consecutive_hits`；当前模型要求连续3次超过阈值才报警。

训练输入是ESP32采集网页保存的批量CSV，默认联合使用`pressure_pa_1`至`pressure_pa_5`。一份CSV可包含数十或数百个`trial_id`；脚本先把每次正向测量拆成独立实验。它自动寻找每次实验末端的共同快速下降，只使用下降之前的因果窗口；有障碍实验中紧邻下降前的向上斜坡为正窗口，更早的平台窗口及无障碍实验窗口为负窗口。`phase`不是`measure`的行和ESP32模拟数据都会被排除。每路无效点按该路有效样本插值；任一所选通道在一次实验中的有效率低于90%时，整次实验排除。

## 安装与运行

```powershell
cd training
python -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
python train.py E:\Research\2026-8-22-pressure --output output
```

`--channels 1,2,3,4,5`是默认值；需要做消融对照时可以指定`--channels 3`或`--channels 3,4`。通道编号必须唯一且位于1～5。

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

默认输入为200 Hz下最近1秒的5路原始压力。网络内部自行完成每路窗口首值扣除、一阶差分、训练集归一化和轻量卷积，因此ONNX只需要`[1,5,1,200]`原始压力输入。均值和标准差已经嵌入模型，不要在Atlas端重复标准化。

## 转换到Atlas

在与板端CANN匹配的ATC环境中执行，并按实际芯片支持列表确认`soc_version`：

```bash
atc --model=output/pressure_ramp.onnx \
    --framework=5 \
    --output=output/pressure_ramp \
    --input_shape="pressure:1,5,1,200" \
    --soc_version=Ascend310B4
```

转换后得到`pressure_ramp.om`。最终验收必须按“整次扫描误报数、障碍召回率、急速下降前提前量”评估，不能只看窗口准确率。
