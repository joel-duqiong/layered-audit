package com.demo.shop.service;

import com.demo.shop.entity.ShopProduct;
import com.demo.shop.repository.ShopProductRepository;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.List;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/** 报表服务 —— 演示高复杂度函数 + 定时任务两个面。 */
@Service
public class ReportService {
    private final ShopProductRepository productRepository;

    public ReportService(ShopProductRepository productRepository) {
        this.productRepository = productRepository;
    }

    /**
     * 聚合报表。
     *
     * ⚠️ 故意埋的缺陷：嵌套三层循环 + 多重条件分支，复杂度高、可读性差，
     * 而且内层循环里还夹着一次数据库访问（N+1 的形状）。
     */
    @Transactional(readOnly = true)
    public List<String> aggregate(String category, boolean onlyInStock) {
        List<String> out = new ArrayList<>();
        for (ShopProduct p : productRepository.findAll()) {
            for (ShopCategoryAlias alias : ShopCategoryAlias.of(p)) {
                if (category != null && !category.equals(alias.name())) {
                    continue;
                }
                if (onlyInStock && p.getStock() != null && p.getStock() <= 0) {
                    continue;
                }
                for (String tag : alias.tags()) {
                    if (tag == null || tag.isBlank()) {
                        continue;
                    }
                    if (p.getPriceCent() != null && p.getPriceCent() > 100000) {
                        out.add(p.getName() + "/" + alias.name() + "/" + tag);
                    } else if (p.getPriceCent() != null) {
                        out.add(p.getName() + "/" + tag);
                    }
                }
            }
        }
        return out;
    }

    /** 定时任务：每天跑一次汇总。 */
    @Scheduled(cron = "0 0 3 * * *")
    public void nightly() {
        aggregate(null, true);
    }

    /** 演示用的辅助类型（放在 service 包内，模拟真实的值对象）。 */
    record ShopCategoryAlias(String name, List<String> tags) {
        static List<ShopCategoryAlias> of(ShopProduct p) {
            if (p.getCategory() == null) {
                return List.of();
            }
            return List.of(new ShopCategoryAlias(p.getCategory().getName(),
                    List.of("goods", p.getStock() != null && p.getStock() > 10 ? "rich" : "poor")));
        }
    }
}
