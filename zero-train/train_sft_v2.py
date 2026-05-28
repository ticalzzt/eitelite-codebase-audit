#!/usr/bin/env python3
"""
0号模型多模态 SFT 训练脚本 — 跑在 GPU 云上
支持: 文本 + 图片理解
基座: Qwen2.5-VL-3B-Instruct (原生多模态)

用法: python3 train_sft.py --data data.jsonl --output ./model-zero-sft
依赖: unsloth, transformers, datasets, trl, qwen-vl-utils
"""
import json
import os
import sys
from pathlib import Path

os.environ["CUDA_VISIBLE_DEVICES"] = "0"

try:
    import torch
    from unsloth import FastLanguageModel, is_bfloat16_supported
    from datasets import Dataset
    from trl import SFTTrainer
    from transformers import TrainingArguments
except ImportError:
    print("请安装依赖: pip install unsloth transformers datasets trl")
    sys.exit(1)


# ============================================================================
# Config
# ============================================================================

class Config:
    # 模型
    base_model = "unsloth/Qwen2.5-VL-3B-Instruct"  # unsloth 预优化版本
    output_dir = "./model-zero-sft"
    
    # 训练
    max_seq_length = 4096  # 多模态需要更长序列
    per_device_batch_size = 2  # 3B 模型 + 图片，显存有限
    gradient_accumulation_steps = 8  # 有效 batch = 2 * 8 = 16
    learning_rate = 2e-4
    num_train_epochs = 3
    warmup_ratio = 0.05
    logging_steps = 1
    save_steps = 100
    
    # LoRA
    lora_r = 16
    lora_alpha = 16
    lora_dropout = 0
    target_modules = ["q_proj", "k_proj", "v_proj", "o_proj",
                      "gate_proj", "up_proj", "down_proj"]


# ============================================================================
# Data loading
# ============================================================================

def load_multimodal_data(path: str) -> list[dict]:
    """
    加载多模态训练数据。
    
    JSONL 格式:
    {
        "instruction": "描述这张图片",
        "input": {"image": "/path/to/image.jpg"},  # 可选
        "output": "图片中显示..."
    }
    
    纯文本格式:
    {
        "instruction": "你是什么模型？",
        "input": {},
        "output": "我是 0 号模型..."
    }
    """
    samples = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                sample = json.loads(line)
            except json.JSONDecodeError:
                continue
            
            instruction = sample.get("instruction", sample.get("prompt", ""))
            output = sample.get("output", sample.get("response", ""))
            input_data = sample.get("input", {})
            
            if not instruction or not output:
                continue
            
            # 检查是否有图片
            image_path = input_data.get("image", "") if isinstance(input_data, dict) else ""
            
            samples.append({
                "instruction": instruction,
                "image": image_path,
                "output": output,
            })
    
    return samples


def format_conversation(sample: dict) -> dict:
    """将样本转为 Qwen2.5-VL 的对话格式。"""
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": sample["instruction"]},
            ]
        },
        {
            "role": "assistant", 
            "content": sample["output"]
        }
    ]
    
    # 如果有图片，添加到 user 消息
    if sample.get("image") and Path(sample["image"]).exists():
        messages[0]["content"].insert(0, {
            "type": "image",
            "image": sample["image"]
        })
    
    return {"messages": messages}


# ============================================================================
# Training
# ============================================================================

def train(config: Config):
    print(f"=== 0号模型多模态 SFT 训练 ===")
    print(f"基座: {config.base_model}")
    print(f"输出: {config.output_dir}")
    
    # 1. 加载模型
    print("\n[1/5] 加载模型...")
    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name=config.base_model,
        max_seq_length=config.max_seq_length,
        dtype=None,  # auto
        load_in_4bit=True,  # 4bit 量化节省显存
    )
    
    # 2. 添加 LoRA
    print("[2/5] 添加 LoRA...")
    model = FastLanguageModel.get_peft_model(
        model,
        r=config.lora_r,
        lora_alpha=config.lora_alpha,
        lora_dropout=config.lora_dropout,
        target_modules=config.target_modules,
        bias="none",
        use_gradient_checkpointing="unsloth",
        random_state=3407,
    )
    
    # 3. 加载数据
    print("[3/5] 加载训练数据...")
    data_path = sys.argv[sys.argv.index("--data") + 1] if "--data" in sys.argv else "data.jsonl"
    samples = load_multimodal_data(data_path)
    print(f"  加载了 {len(samples)} 个样本")
    
    # 统计
    with_images = sum(1 for s in samples if s.get("image"))
    print(f"  其中 {with_images} 个包含图片")
    
    # 转为对话格式
    conversations = [format_conversation(s) for s in samples]
    dataset = Dataset.from_list(conversations)
    
    # 4. 训练
    print("[4/5] 开始训练...")
    output_dir = sys.argv[sys.argv.index("--output") + 1] if "--output" in sys.argv else config.output_dir
    
    training_args = TrainingArguments(
        output_dir=output_dir,
        per_device_train_batch_size=config.per_device_batch_size,
        gradient_accumulation_steps=config.gradient_accumulation_steps,
        learning_rate=config.learning_rate,
        num_train_epochs=config.num_train_epochs,
        warmup_ratio=config.warmup_ratio,
        logging_steps=config.logging_steps,
        save_steps=config.save_steps,
        fp16=not is_bfloat16_supported(),
        bf16=is_bfloat16_supported(),
        optim="adamw_8bit",
        save_total_limit=3,
        report_to="none",
        remove_unused_columns=False,
    )
    
    trainer = SFTTrainer(
        model=model,
        tokenizer=tokenizer,
        train_dataset=dataset,
        args=training_args,
        max_seq_length=config.max_seq_length,
        packing=True,
    )
    
    trainer.train()
    
    # 5. 保存
    print("[5/5] 保存模型...")
    model.save_pretrained(output_dir)
    tokenizer.save_pretrained(output_dir)
    
    print(f"\n✅ 训练完成！模型保存在: {output_dir}")
    print(f"  参数量: {model.num_parameters():,}")
    print(f"  训练样本: {len(samples)}")
    print(f"  图片样本: {with_images}")


# ============================================================================
# Main
# ============================================================================

if __name__ == "__main__":
    config = Config()
    train(config)
