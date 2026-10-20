import type { ChatResponse, ProductCard } from '../api/types';

export function product(sku = 'qiguang-l1'): ProductCard {
  return { sku, name: `栖光 ${sku} 桌面台灯`, category: '桌面台灯',
    highlights: ['额定功率：6 瓦', '色温档位：3000、4000、5000 开尔文'],
    image_url: `https://images.example.com/products/${sku}/main.webp` };
}

export function productAnswer(products?: ProductCard[]): ChatResponse {
  return { request_id: 'r', session_id: 's', answer: '栖光台灯的额定功率为 6 瓦。', route: 'rag',
    strategy: 'direct', sources: [], is_mock: false, trace: null,
    ...(products ? { products } : {}) };
}
