package com.demo.shop.web;

import com.demo.shop.entity.ShopProduct;
import com.demo.shop.service.ProductService;
import com.demo.shop.web.dto.ProductResponse;
import java.util.List;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/products")
public class ProductController {
    private final ProductService productService;

    public ProductController(ProductService productService) {
        this.productService = productService;
    }

    @GetMapping
    public List<ProductResponse> list() {
        return productService.listCached();
    }

    /** 返回内部实体的 id 列表（避免序列化懒加载关联）。 */
    @GetMapping("/internal")
    public List<Long> internal() {
        return productService.snapshot().stream().map(ShopProduct::getId).toList();
    }
}
