package com.demo.shop.repository;

import com.demo.shop.entity.ShopCategory;
import org.springframework.data.jpa.repository.JpaRepository;

public interface ShopCategoryRepository extends JpaRepository<ShopCategory, Long> {
}
