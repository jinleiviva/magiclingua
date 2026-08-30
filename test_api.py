#!/usr/bin/env python3
"""
HY-MT API测试脚本
测试翻译服务是否正常工作
"""

import requests
import json
import sys

API_URL = "http://localhost:18770/v1/chat/completions"

def test_translation(source_text, target_language, source_language="zh"):
    """
    测试翻译功能
    
    Args:
        source_text: 源文本
        target_language: 目标语言（中文名称）
        source_language: 源语言代码，默认为中文
    """
    # 构建prompt
    if source_language == "zh":
        # 中文到其他语言
        prompt = f"将以下文本翻译为{target_language}，注意只需要输出翻译后的结果，不要额外解释：{source_text}"
    else:
        # 其他语言到其他语言（非中文）
        prompt = f"Translate the following segment into {target_language}, without additional explanation. {source_text}"
    
    # 构建请求
    payload = {
        "model": "hunyuan-mt",
        "messages": [
            {
                "role": "user",
                "content": prompt
            }
        ],
        "max_tokens": 2048,
        "temperature": 0.7,
        "top_p": 0.6,
        "top_k": 20,
        "repetition_penalty": 1.05
    }
    
    try:
        print(f"\n{'='*50}")
        print(f"测试: {source_text[:50]}...")
        print(f"目标语言: {target_language}")
        print(f"{'='*50}")
        
        response = requests.post(API_URL, json=payload, timeout=60)
        response.raise_for_status()
        
        result = response.json()
        translation = result['choices'][0]['message']['content']
        
        print(f"✓ 翻译结果: {translation}")
        return translation
        
    except requests.exceptions.ConnectionError:
        print("✗ 错误: 无法连接到服务器")
        print("  请确保服务器正在运行: ./start_server_gguf.sh")
        sys.exit(1)
    except requests.exceptions.Timeout:
        print("✗ 错误: 请求超时")
        sys.exit(1)
    except Exception as e:
        print(f"✗ 错误: {e}")
        sys.exit(1)

def main():
    print("\n" + "="*50)
    print("HY-MT 翻译API测试")
    print("="*50)
    
    # 测试1: 中文到英文
    test_translation(
        "你好，世界！这是一个测试。",
        "English"
    )
    
    # 测试2: 英文到中文
    test_translation(
        "Hello, world! This is a test.",
        "中文",
        source_language="en"
    )
    
    # 测试3: 中文到日文
    test_translation(
        "人工智能正在改变我们的世界。",
        "Japanese"
    )
    
    # 测试4: 长文本翻译
    test_translation(
        "机器翻译是计算语言学的一个分支，是人工智能的终极目标之一，具有重要的科学研究价值。",
        "English"
    )
    
    print("\n" + "="*50)
    print("✓ 所有测试通过！")
    print("="*50 + "\n")

if __name__ == "__main__":
    main()
