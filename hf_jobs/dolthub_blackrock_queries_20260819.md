# Frozen read-only DoltHub queries for US ETF rotation R0

Source database: `deeleeramone/blackrock-public`, branch `main`. These links are used only to discover schema and extract the pre-declared iShares target funds.

- [SHOW TABLES](https://www.dolthub.com/api/v1alpha1/deeleeramone/blackrock-public/main?q=SHOW%20TABLES)
- [DESCRIBE funds](https://www.dolthub.com/api/v1alpha1/deeleeramone/blackrock-public/main?q=DESCRIBE%20funds)
- [DESCRIBE nav_history](https://www.dolthub.com/api/v1alpha1/deeleeramone/blackrock-public/main?q=DESCRIBE%20nav_history)
- [DESCRIBE distributions](https://www.dolthub.com/api/v1alpha1/deeleeramone/blackrock-public/main?q=DESCRIBE%20distributions)
- [Target funds](https://www.dolthub.com/api/v1alpha1/deeleeramone/blackrock-public/main?q=SELECT%20%2A%20FROM%20funds%20WHERE%20ticker%20IN%20%28%27IWM%27%2C%27IWF%27%2C%27IWD%27%2C%27MTUM%27%2C%27QUAL%27%2C%27USMV%27%2C%27VLUE%27%29)
- [Target NAV coverage](https://www.dolthub.com/api/v1alpha1/deeleeramone/blackrock-public/main?q=SELECT%20portfolio_id%2C%20MIN%28as_of_date%29%20min_date%2C%20MAX%28as_of_date%29%20max_date%2C%20COUNT%28%2A%29%20rows_count%20FROM%20nav_history%20WHERE%20portfolio_id%20IN%20%28SELECT%20portfolio_id%20FROM%20funds%20WHERE%20ticker%20IN%20%28%27IWM%27%2C%27IWF%27%2C%27IWD%27%2C%27MTUM%27%2C%27QUAL%27%2C%27USMV%27%2C%27VLUE%27%29%29%20GROUP%20BY%20portfolio_id%20ORDER%20BY%20portfolio_id)
- [Target distribution coverage](https://www.dolthub.com/api/v1alpha1/deeleeramone/blackrock-public/main?q=SELECT%20portfolio_id%2C%20MIN%28ex_date%29%20min_date%2C%20MAX%28ex_date%29%20max_date%2C%20COUNT%28%2A%29%20rows_count%20FROM%20distributions%20WHERE%20portfolio_id%20IN%20%28SELECT%20portfolio_id%20FROM%20funds%20WHERE%20ticker%20IN%20%28%27IWM%27%2C%27IWF%27%2C%27IWD%27%2C%27MTUM%27%2C%27QUAL%27%2C%27USMV%27%2C%27VLUE%27%29%29%20GROUP%20BY%20portfolio_id%20ORDER%20BY%20portfolio_id)
