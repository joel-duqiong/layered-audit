package com.demo.shop.service;

import com.demo.shop.entity.ShopProduct;
import com.demo.shop.repository.ShopProductRepository;
import com.demo.shop.web.dto.ProductResponse;
import java.util.List;
import org.springframework.cache.annotation.CacheEvict;
import org.springframework.cache.annotation.Cacheable;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/** 商品服务 —— 演示缓存 + 封装泄漏两个面。 */
@Service
public class ProductService {
    private final ShopProductRepository productRepository;

    public ProductService(ShopProductRepository productRepository) {
        this.productRepository = productRepository;
    }

    /**
     * 列表（走缓存）。
     *
     * ⚠️ 故意埋的缺陷：缓存里放的是**可变对象**，调用方拿到就能改，
     * 改的是缓存本身 —— 后续所有请求都读到被改过的值。
     */
    @Transactional(readOnly = true)
    @Cacheable("products")
    public List<ProductResponse> listCached() {
        return productRepository.findAll().stream()
                .map(p -> new ProductResponse(p.getId(), p.getName(), p.getPriceCent(),
                        p.getStock(), p.getCategory() == null ? null : p.getCategory().getName()))
                .toList();
    }

    /**
     * ⚠️ 故意埋的缺陷：把**内部可变实体**直接交给调用方。
     * 调用方一改，内部状态就跟着变 —— 服务层的封装被穿透。
     */
    @Transactional(readOnly = true)
    public List<ShopProduct> snapshot() {
        return productRepository.findAll();
    }

    @Transactional
    @CacheEvict("products")
    public void evict() {
        // 演示用：真实项目里这里是改价 / 下架
    }
}
