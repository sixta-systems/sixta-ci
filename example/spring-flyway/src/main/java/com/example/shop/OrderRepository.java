package com.example.shop;

import java.util.List;

import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.CrudRepository;

/**
 * Embedded-SQL demo: the native query below is extracted and analyzed
 * (leading-wildcard LIKE defeats the index); the JPQL query is skipped.
 */
public interface OrderRepository extends CrudRepository<Order, Long> {

    @Query(value = "SELECT * FROM orders WHERE status LIKE '%' || :suffix", nativeQuery = true)
    List<Order> byStatusSuffix(String suffix);

    @Query("SELECT o FROM Order o WHERE o.status = ?1") // JPQL: not SQL, not analyzed
    List<Order> byStatus(String status);
}
