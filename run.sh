CUDA_VISIBLE_DEVICES=0 nohup python3 condense.py --reproduce -d cifar10 -f 2 --ipc 10 --dp C > output1.log 2>&1 &