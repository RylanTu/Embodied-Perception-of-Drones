# 模型文件

`pressure_causal_tcn_ch234.onnx`固定使用2、3、4号压力传感器，输入形状为
`[1,3,1,200]`，输出为单个logit。把完整`atlas`目录复制到Atlas板后运行：

```bash
chmod +x deploy_ch234.sh run.sh
./deploy_ch234.sh
```

脚本调用板端ATC生成`pressure_causal_tcn_ch234.om`，随后更新`config.json`并启用
模型和障碍自动停止。当连续3次推理达到阈值时确认障碍。起步豁免距离为缓启动距离再加100 mm。自动批次中检测到障碍后会立即停止并结束批次，保持停止锁定；
不会自动回位或继续下一次，模拟传感器数据不会触发停止。
