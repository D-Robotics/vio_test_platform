# test_platform — VIO 数据管理与算法回测平台

自包含的 FastAPI + 原生 JS 单页应用，用于管理 `ros2bag_vio` + `stereo_auto_gen` 数据集、预览数据、编辑配置、驱动 X5 板子做 VIO 回测。

## 启动

```bash
bash test_platform/run.sh              # 自动检查/补装依赖 + 启动，http://localhost:1234
bash test_platform/run.sh --check      # 只检查依赖/目录，不启动
bash test_platform/run.sh --install    # 只补装依赖，不启动
```

`run.sh` 会：创建 `results/`、`state/` 目录（`main.py` 启动时用 `StaticFiles` 挂载 `/results`，缺目录会起不来）；
`pip --target vendor_libs/` 检测并补装 `requirements.txt` + matplotlib（轨迹图用）；`ffmpeg` 缺失时提示（录像可回落到板端 ffmpeg）。

`start.sh` 等价于 `run.sh`（仅作兼容保留）。依赖装进 `test_platform/vendor_libs/`（不污染系统环境）。
数据根目录默认 `/home/hobot/work/cc_ws/tros_ws`（`DATA_ROOT` 环境变量可改），端口默认 1234（`PORT` 可改）。

## 功能

| Tab | 功能 |
|---|---|
| 数据集 | 扫描 DATA_ROOT 下的数据集（含 `ros2bag_vio/` 或 `stereo_auto_gen/` 的目录）；选中即进入**播放器**：所有 topic 同时播放——图像 topic 按帧序列播放（视频化）、IMU/Odom/TF/GPS 为滚动曲线/轨迹窗口；每个 topic 一个窗口，chip 点击 hide/show；进度条拖拉 seek；播放/暂停 + 0.25x~4x 速率 |
| 配置编辑 | 读写 stereo_auto_gen 下的 yaml；保存前解析校验（OpenCV `%YAML:1.0` 头宽容处理），写入前自动 `.bak` 备份 |
| 板子管理 | 板列表（boards.json，密码只存服务端）；SSH 连通性测试 |
| 回测 | 选择板+数据集 → 自动检测/挂载 NFS（复用板上已有挂载，如 /mnt/nfs20）→ SSH 启动全链（static TF×2 + ov_web + VIO launch + bag play，launch 参数按 bag topic 自动映射）→ 进程状态灯 + 日志滚动 → 内嵌 ov_web（板:9988）iframe；一键停止清理全部进程 |
| 批量回测 | 回测 Tab 内多选数据集 → 顺序执行（每项等 bag 播完/VIO 退出自动进入下一项）→ 每项结束 SFTP 自动收集结果（ov_est.tum/state/结果bag）到 `test_platform/results/<批次>/` |
| 统计 | 统计 Tab：每数据集一张卡片——数据名、首帧预览图、轨迹图（matplotlib 渲染）、**ov_web 可视化录像（video.mp4，浏览器内直接播放）**、起点/终点坐标、路程、时长、位姿数、状态/错误；结果持久化（_meta.json），服务重启不丢 |

## API（/api 前缀，见 server/main.py）

datasets / config / boards / env / backtest(start|stop|status|mount) / batch(start|status|stop) / results / player(prepare|slice)。

播放器：`prepare` 一次性构建各 topic 时间索引（缓存）；`slice?images_only=1` 纯二分查最近帧（~1ms），序列数据由前端预载全量后本地开窗，保证拖拉顺滑。

## NFS 说明

若主机尚未导出 NFS：`sudo bash test_platform/setup_nfs.sh`（一次性）。
若板子已挂载主机文件系统（如 `/mnt/nfs20`），平台自动复用，无需重新挂载。

## 板端环境要求（重要）

SSH 启动的命令自动 `source /opt/tros/humble/setup.bash` + `/userdata/demo/install/setup.bash`。
**板上安装的 drobotics_vio 需为较新版本**（含 `subscribe.launch.py` + `ov_web`）：
2026-08-28 在 192.168.1.15 上验证时发现该板固件较旧——compressed 订阅 topic 硬编码为
`/sub_image_combine_jjpeg`（typo，不读 launch 参数）且无 `ov_web.launch.py`，导致
VIO 收不到图像。VIO 节点、bag 播放、TF、进程管理、日志回传均验证工作正常；
更新板端固件后全链即可闭环。

### ov_web 可视化与录像

- 实时：回测 Tab 的 iframe 直连 `http://<板IP>:9988/`（ov_web，板端运行）。
- 录像：批量回测运行期间，平台在主机侧连接 `ws://<板IP>:9988/ws` 录制 ov_web 推送的
  JPEG 可视化帧，结束后 ffmpeg 合成 `video.mp4`（h264, 15fps），原始帧目录自动清理，
  视频在统计 Tab 卡片内直接播放。录制时会把 JSON 里的 odom 轨迹解析成一张**俯视轨迹小地图**
  叠加到每帧右下角，视频里即可看到机器人行走轨迹。（`record.py` `OvWebRecorder`，`minimap=False` 可关闭。）
