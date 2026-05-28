#!/usr/bin/env python3
"""
0号模型 GRPO 训练脚本 — 强化学习对齐
用 SFT 模型作为起点，用验证信号做 reward

用法: python3 train_grpo.py --model ./model-zero-sft --data data.jsonl --output ./model-zero-grpo
依赖: unsloth, trl, transformers
"""
import json
import os
import sys
from pathlib import Path

os.environ["CUDA_VISIBLE_DEVICES"] = "0"

try:
    import torch
    from unsloth import FastLanguageModel, is_bfloat16_supported
    from trl import GRPOConfig, GRPOTrainer
    from datasets import Dataset
except ImportError:
    print("请安装依赖: pip install unsloth trl transformers")
    sys.exit(1)


# ============================================================================
# Reward functions — 基于 EITE 验证信号
# ============================================================================

def reward_no_hallucination(prompts, completions, **kwargs) -> list[float]:
    """奖励不编造的回复。"""
    scores = []
    for completion in completions:
        text = completion if isinstance(completion, str) else completion.get("content", "")
        score = 1.0
        
        # 惩罚编造模型名
        fake_model_patterns = ["deepseek-v4", "gpt-4", "claude-3", "qwen-max"]
        for pattern in fake_model_patterns:
            if pattern in text.lower():
                score -= 0.5
        
        # 她励使用 check_self
        if "check_self" in text or "config_model" in text:
            score += 0.3
        
        scores.append(max(0.0, score))
    return scores


def reward_evidence_chain(prompts, completions, **kwargs) -> list[float]:
    """奖励有证据链的回复。"""
    scores = []
    for completion in completions:
        text = completion if isinstance(completion, str) else completion.get("content", "")
        score = 1.0
        
        # 奖励包含工具调用结果
        if "```" in text or "bash" in text.lower():
            score += 0.2
        
        # 奖励引用具体数据
        if any(x in text for x in ["commit", "file", "line", "test", "pass", "fail"]):
            score += 0.2
        
        # 惩罚纯声明无证据
        claim_words = ["已保存", "已创建", "已修复", "saved", "created", "fixed"]
        for word in claim_words:
            if word in text.lower() and "```" not in text:
                score -= 0.3
        
        scores.append(max(0.0, min(2.0, score)))
    return scores


def reward_concise(prompts, completions, **kwargs) -> list[float]:
    """奖励简洁的回复。"""
    scores = []
    for completion in completions:
        text = completion if isinstance(completion, str) else completion.get("content", "")
        length = len(text)
        
        if length < 100:
            score = 1.5  # 简洁
        elif length < 500:
            score = 1.0  # 适中
        elif length < 1000:
            score = 0.7  # 稍长
        else:
            score = 0.3  # 太长
        
        scores.append(score)
    return scores


def reward_chinese_support(prompts, completions, **kwargs) -> list[float]:
    """奖励中英文混合能力。"""
    scores = []
    for completion in completions:
        text = completion if isinstance(completion, str) else completion.get("content", "")
        
        has_chinese = any('\u4e00' <= c <= '\u9fff' for c in text)
        has_english = any(c.isalpha() and ord(c) < 128 for c in text)
        
        if has_chinese and has_english:
            score = 1.2  # 中英混合
        elif has_chinese:
            score = 1.0  # 纯中文
        elif has_english:
            score = 1.0  # 纯英文
        else:
            score = 0.5  # 无文字
        
        scores.append(score)
    return scores


# ============================================================================
# Combined reward
# ============================================================================

def combined_reward(prompts, completions, **kwargs) -> list[float]:
    """组合所有 reward 函数。"""
    r1 = reward_no_hallucination(prompts, completions, **kwargs)
    r2 = reward_evidence_chain(prompts, completions, **kwargs)
    r3 = reward_concise(prompts, completions, **kwargs)
    r4 = reward_chinese_support(prompts, completions, **kwargs)
    
    return [a + b + c + d for a, b, c, d in zip(r1, r2, r3, r4)]


# ============================================================================
# Config
# ============================================================================

class Config:
    base_model = "./model-zero-sft"  # SFT 后的模型
    output_dir = "./model-zero-grpo"
    max_seq_length = 4096
    per_device_batch_size = 2
    gradient_accumulation_steps = 4
    learning_rate = 5e-6  # GRPO 用更小的学习率
    num_train_epochs = 1
    max_completion_length = 1024
    num_generations = 4  # 每个 prompt 生成 4 个候选
    beta = 0.04  # KL 散度系数


# ============================================================================
# Main
# ============================================================================

def train(config: Config):
    print(f"=== 0号模型 GRPO 训练 ===")
    print(f"基座: {config.base_model}")
    
    # 1. 加载 SFT 模型
    print("\n[1/4] 加载 SFT 模型...")
    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name=config.base_model,
        max_seq_length=config.max_seq_length,
        dtype=None,
        load_in_4bit=True,
    )
    
    # 2. 加载数据
    print("[2/4] 加载训练数据...")
    data_path = sys.argv[sys.argv.index("--data") + 1] if "--data" in sys.argv else "data.jsonl"
    prompts = []
    with open(data_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                sample = json.loads(line)
                prompt = sample.get("instruction", sample.get("prompt", ""))
                if prompt:
                    prompts.append(prompt)
            except:
                continue
    
    print(f"  加载了 {len(prompts)} 个 prompts")
    dataset = Dataset.from_dict({"prompt": prompts})
    
    # 3. 训练
    print("[3/4] 开始 GRPO 训练...")
    output_dir = sys.argv[sys.argv.index("--output") + 1] if "--output" in sys.argv else config.output_dir
    
    training_args = GRPOConfig(
        output_dir=output_dir,
        per_device_train_batch_size=config.per_device_batch_size,
        gradient_accumulation_steps=config.gradient_accumulation_steps,
        learning_rate=config.learning_rate,
        num_train_epochs=config.num_train_epochs,
        max_completion_length=config.max_completion_length,
        num_generations=config.num_generations,
        beta=config.beta,
        logging_steps=1,
        save_steps=50,
        fp16=not is_bfloat16_supported(),
        bf16=is_bfloat16_supported(),
        optim="adamw_8bit",
        save_total_limit=3,
        report_to="none",
        remove_unused_columns=False,
    )
    
    trainer = GRPOTrainer(
        model=model,
        processing_class=tokenizer,
        reward_funcs=[combined_reward],
        args=training_args,
        train_dataset=dataset,
    )
    
    trainer.train()
    
    # 4. 保存
    print("[4/4] 保存模型...")
    model.save_pretrained(output_dir)
    tokenizer.save_pretrained(output_dir)
    
    print(f"\n✅ GRPO 训练完成！模型保存在: {output_dir}")


if __name__ == "__main__":
    config = Config()
    train(config)
