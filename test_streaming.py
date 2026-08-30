#!/usr/bin/env python3
"""
测试流式输出功能
"""

import requests
import json

API_URL = "http://localhost:18770/v1/chat/completions"

def test_streaming():
    """测试流式翻译"""
    print("=" * 60)
    print("测试流式输出功能")
    print("=" * 60)
    
    prompt = "将以下文本翻译为English，注意只需要输出翻译后的结果，不要额外解释：人工智能正在改变我们的世界，它为我们带来了前所未有的机遇和挑战。"
    
    # 发送流式请求
    response = requests.post(
        API_URL,
        headers={"Content-Type": "application/json"},
        json={
            "model": "hunyuan-mt",
            "messages": [
                {"role": "user", "content": prompt}
            ],
            "max_tokens": 500,
            "stream": True  # 启用流式输出
        },
        stream=True  # 重要：requests 也要启用 stream
    )
    
    print("\n流式翻译结果：\n")
    print("-" * 60)
    
    # 逐块接收并显示
    for line in response.iter_lines():
        if line:
            line = line.decode('utf-8')
            if line.startswith('data: '):
                data_str = line[6:]  # 移除 "data: " 前缀
                
                if data_str == '[DONE]':
                    break
                
                try:
                    data = json.loads(data_str)
                    if 'choices' in data and data['choices']:
                        content = data['choices'][0].get('delta', {}).get('content', '')
                        if content:
                            print(content, end='', flush=True)
                except json.JSONDecodeError:
                    pass
    
    print("\n" + "-" * 60)
    print("✓ 流式翻译完成！")

def test_non_streaming():
    """测试非流式翻译（对比）"""
    print("\n" + "=" * 60)
    print("测试非流式输出功能（对比）")
    print("=" * 60)
    
    prompt = "将以下文本翻译为English，注意只需要输出翻译后的结果，不要额外解释：你好，世界！"
    
    response = requests.post(
        API_URL,
        headers={"Content-Type": "application/json"},
        json={
            "model": "hunyuan-mt",
            "messages": [
                {"role": "user", "content": prompt}
            ],
            "max_tokens": 200,
            "stream": False  # 非流式
        }
    )
    
    data = response.json()
    translation = data['choices'][0]['message']['content']
    
    print("\n非流式翻译结果：")
    print("-" * 60)
    print(translation)
    print("-" * 60)
    print("✓ 非流式翻译完成！")

if __name__ == "__main__":
    import time
    
    print("\n\n🌟 HY-MT 流式输出测试\n\n")
    
    # 测试流式输出
    test_streaming()
    
    time.sleep(1)
    
    # 测试非流式输出（对比）
    test_non_streaming()
    
    print("\n\n🎉 所有测试完成！\n")
