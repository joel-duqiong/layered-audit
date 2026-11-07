package com.demo.shop.repository;

import com.demo.shop.entity.ShopCustomer;
import java.util.Optional;
import org.springframework.data.jpa.repository.JpaRepository;

public interface ShopCustomerRepository extends JpaRepository<ShopCustomer, Long> {
    Optional<ShopCustomer> findByName(String name);
}
