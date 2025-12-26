import pyomo.environ as pyo

def build_model(
    days,
    recipe_dict,
    recipe_ids,
    recipeitem_dict,
    filtered_recipe_nutritions,
    nutritionaltarget_dict,
    itemweight_dict,
    itemequal_dict,
    menstruation,
    regist_item,
    use_pfc=True
):
     # Pyomo の具体モデルを生成
    model = pyo.ConcreteModel()

    # 日とレシピIDの集合
    model.Days = pyo.Set(initialize=days)
    model.Recipes = pyo.Set(initialize=recipe_ids)

    # --- kind1, kind2 用の補助パラメータ ---
    # レシピごとの kind1（staple, main, side, soup など）を Param に保持
    def kind1_map_init(m, r):
        return recipe_dict[r]['data']['kind1']
    model.kind1_map = pyo.Param(model.Recipes, initialize=kind1_map_init, within=pyo.Any)

    # レシピごとの kind2（ご飯もの, パスタ, カレー など）を Param に保持
    def kind2_map_init(m, r):
        return recipe_dict[r]['data']['kind2']
    model.kind2_map = pyo.Param(model.Recipes, initialize=kind2_map_init, within=pyo.Any)

    # --- kind2 が {ご飯もの, パスタ, カレー, 鍋} の主食レシピ集合 ---
    staple_special_kind2 = {'ご飯もの', 'パスタ', 'カレー', '鍋'}
    model.StapleSpecialRecipes = pyo.Set(initialize=[r for r in recipe_ids if recipe_dict[r]['data']['kind1']=='staple' and recipe_dict[r]['data']['kind2'] in staple_special_kind2])

    # --- 変数定義 ---
    # 日×レシピの採用フラグ（そのレシピをその日に使うかどうか）
    model.x = pyo.Var(model.Days, model.Recipes, domain=pyo.Binary)

    # すべてのレシピから登場する食材名の集合を作成
    all_ingredients = set()
    for items in recipeitem_dict.values():
        all_ingredients.update(k for k, v in items.items() if v > 0)
    model.Ingredients = pyo.Set(initialize=sorted(all_ingredients))

    # 食材ごとの「使ったかどうか」を表すバイナリ変数
    all_items = sorted({
        item
        for r, rec in recipeitem_dict.items()
        for item in rec.keys()
    })
    model.Items = pyo.Set(initialize=all_items)
    model.item_used = pyo.Var(model.Items, domain=pyo.Binary)

    # 週全体で使った食材種類（Ingredients ベース）を判定するバイナリ変数
    model.y_item = pyo.Var(model.Ingredients, domain=pyo.Binary) 

    # --- 1日あたりの品目構成に関する制約 ---
    # 各日，主食は 1 品
    def staple_count_rule(m, d):
        return sum(m.x[d, r] for r in model.Recipes if m.kind1_map[r] == 'staple') == 1
    model.StapleCount = pyo.Constraint(model.Days, rule=staple_count_rule)

    # 各日，主菜は「通常主菜 1 品」または「ご飯もの/パスタ/カレー/鍋の主食 1 品」のどちらか
    def main_count_rule(m, d):
         # その日に選ばれた「特別主食」の数
        staple_special_sum = sum(m.x[d, r] for r in model.StapleSpecialRecipes)
        # main の数 + 特別主食の数 = 1 になるよう制約
        return sum(m.x[d, r] for r in model.Recipes if m.kind1_map[r] == 'main') + staple_special_sum == 1
    model.MainCount = pyo.Constraint(model.Days, rule=main_count_rule)

    # 各日，副菜は 1 品
    def side_count_rule(m, d):
        return sum(m.x[d, r] for r in model.Recipes if m.kind1_map[r] == 'side') == 1
    model.SideCount = pyo.Constraint(model.Days, rule=side_count_rule)

    # 各日，汁物は 1 品
    def soup_count_rule(m, d):
        return sum(m.x[d, r] for r in model.Recipes if m.kind1_map[r] == 'soup') == 1
    model.SoupCount = pyo.Constraint(model.Days, rule=soup_count_rule)

    # --- 栄養制約の準備 ---
    # nutritionaltarget_dict から対象ユーザの栄養目標を 1 行取り出す
    def recipe_usage_rule(m, r):
        if r in m.StapleSpecialRecipes:
            return sum(m.x[d, r] for d in m.Days) <= 7
        else:
            return sum(m.x[d, r] for d in m.Days) <= 1
    model.RecipeUsage = pyo.Constraint(model.Recipes, rule=recipe_usage_rule)

    # （カロリー用）制約調整用の値
    target = next(iter(nutritionaltarget_dict.values()))
    nutritionals = target['nutritionals']  

    cal_val = nutritionals.get('カロリー', None)

    # PFC とその他の栄養素のキー（RecipeNutrition 側の名前）
    pfc_keys = [
        'カロリー(kcal)',
        'たんぱく質(g)',
        '脂質(g)',
        '炭水化物(g)',
    ]

    other_keys = [
        "食物繊維(g)",
        "カルシウム(mg)",
        "ビタミンA(μg)",
        "ビタミンD(μg)",
        "ビタミンC(mg)",
        "ビタミンB₁(mg)",
        "ビタミンB₂(mg)",
        "鉄(mg)"
    ]

    # 実際にモデルに入れる栄養素のリスト
    if use_pfc:
        nut_keys = pfc_keys + other_keys
    else:
        nut_keys = other_keys

    # 栄養制約：各栄養素について 1 週間の合計が目標範囲に収まるようにする
    def nutrition_rule(m, nut):
        total_val = sum(
            m.x[d, r] * filtered_recipe_nutritions[r].get(nut, 0)
            for d in m.Days
            for r in m.Recipes
        )

        # 許容するズレ：目標値の5%　※カロリー・PFC・塩分以外の上限値は超えてはいけないラインなので下限値のみ範囲変更
        low_ratio = 0.95 
        up_ratio = 1.05

        if nut == 'カロリー(kcal)':
            lower = cal_val * 0.9 * low_ratio
            upper = cal_val * 1.1 * up_ratio
            return pyo.inequality(lower, total_val, upper)

        if nut == 'たんぱく質(g)':
            p_lb = cal_val * low_ratio * (nutritionals.get('たんぱく質_下限',0)/100) / 4
            p_ub = cal_val * up_ratio * (nutritionals.get('たんぱく質_上限',0)/100) / 4
            return pyo.inequality(p_lb, total_val, p_ub)

        if nut == '脂質(g)':
            f_lb = cal_val * low_ratio * (nutritionals.get('脂質_下限',0)/100) / 9
            f_ub = cal_val * up_ratio * (nutritionals.get('脂質_上限',0)/100) / 9
            return pyo.inequality(f_lb, total_val, f_ub)

        if nut == '炭水化物(g)':
            c_lb = cal_val * low_ratio * (nutritionals.get('炭水化物_下限',0)/100) / 4
            c_ub = cal_val * up_ratio * (nutritionals.get('炭水化物_上限',0)/100) / 4
            return pyo.inequality(c_lb, total_val, c_ub)

        # それ以外の栄養素
        lower_key = f"{nut.split('(')[0]}_下限"
        upper_key = f"{nut.split('(')[0]}_上限"
        raw_lower = nutritionals.get(lower_key, None)
        raw_upper = nutritionals.get(upper_key, None)

        # 鉄の月経対応
        if nut == '鉄(mg)':
            if menstruation == 'あり':
                raw_lower = nutritionals.get('鉄・月経時_下限', None)
            else:
                raw_lower = nutritionals.get('鉄_下限', None)
            raw_upper = nutritionals.get('鉄_上限', None)

        # 下限側
        if raw_lower is not None:
            raw_lower = raw_lower * low_ratio   # 下限を少し緩める
        else:
            raw_lower = None

        if raw_lower is None and raw_upper is None:
            return pyo.Constraint.Skip
        if raw_lower is None:
            return pyo.inequality(None, total_val, raw_upper)
        if raw_upper is None:
            return pyo.inequality(raw_lower, total_val, None)
        return pyo.inequality(raw_lower, total_val, raw_upper)

    model.NutritionConstraints = pyo.Constraint(nut_keys, rule=nutrition_rule)

    # 1日の品目数（選ばれたレシピ数）を 3〜4 個に制限
    def items_per_day_rule(m, d):
        return pyo.inequality(3, sum(m.x[d, r] for r in m.Recipes), 4)

    model.ItemsPerDay = pyo.Constraint(model.Days, rule=items_per_day_rule)

    # 未使用量（使い残し）を表す変数
    model.Unused = pyo.Var(regist_item.keys(), domain=pyo.NonNegativeReals)

    # 登録食材を使ったかどうか（0/1）
    model.y_regist = pyo.Var(model.Ingredients, domain=pyo.Binary)
    def y_regist_rule(m, i):
        # 1週間のどこかで i が使われていたら 1
        total_used = sum(
            m.x[d, r] * recipeitem_dict[r].get(i, 0)
            for d in m.Days for r in m.Recipes
        )
        # total_used > 0 → y_regist[i] = 1 を言いたい
        # Pyomo では Big-M の形にする
        return total_used <= BIG_M * m.y_regist[i]

    BIG_M = 1000
    model.YRegistConstraint = pyo.Constraint(model.Ingredients, rule=y_regist_rule)

    # 指定食材の使用量を「基準重量の倍数」に近づけるための誤差変数 e[d,r,i]
    model.e = pyo.Var(model.Days, model.Recipes, model.Ingredients, within=pyo.NonNegativeReals)

    # 実際の使用量と「最も近い倍数」との差に対する片側制約1
    def multiple_soft_rule(m, d, r, i):
        # 倍数ルールの対象外の食材はスキップ
        if i not in itemweight_dict:
            return pyo.Constraint.Skip

        # 一つ目の重さ（基準重量）
        weight_list = itemweight_dict[i].get("weights", [])
        if not weight_list:
            return pyo.Constraint.Skip
        weight = weight_list[0]

        # レシピで使う食材量（g） ※ここは整数
        amount = recipeitem_dict[r].get(i, 0)

        # x[d,r] = 0 → 使わない → 誤差も 0
        if amount == 0:
            return m.e[d, r, i] >= 0

        # 最も近い weight の倍数
        mult = round(amount / weight)

        # 誤差は以下を満たす必要あり
        return m.e[d, r, i] >= m.x[d, r] * amount - (weight * mult)

    # 片側制約2（逆側からの差を抑える）
    def multiple_soft_rule2(m, d, r, i):
        if i not in itemweight_dict:
            return pyo.Constraint.Skip

        weight = itemweight_dict[i]["weights"][0]
        amount = recipeitem_dict[r].get(i, 0)

        return m.e[d,r,i] >= weight * round(amount / weight) - m.x[d,r] * amount

    model.MultipleSoft1 = pyo.Constraint(model.Days, model.Recipes, model.Ingredients, rule=multiple_soft_rule)
    model.MultipleSoft2 = pyo.Constraint(model.Days, model.Recipes, model.Ingredients, rule=multiple_soft_rule2)

    # 献立に使用する食材の種類の数を数える
    def ingredient_link_rule(m, i):
        # i が使われたら y_item[i] = 1 になる制約
        return sum(
            m.x[d, r] * recipeitem_dict[r].get(i, 0)
            for d in m.Days for r in m.Recipes
        ) <= 1e6 * m.y_item[i]

    model.IngredientLink = pyo.Constraint(model.Ingredients, rule=ingredient_link_rule)

    # 各食材を代表名にマップする辞書
    eq_dict = {}
    for name, d in itemequal_dict.items():
        rep = name              # DB上の代表（白米 or 卵）
        eq  = d["equals"]       # 等価な1食材名（ご飯 or ゆで卵）

        eq_dict.setdefault(rep, set()).add(rep)
        eq_dict[rep].add(eq)

    # ここで「白米クラスに米を足す」ブリッジを追加する
    if "白米" in eq_dict:
        eq_dict["白米"].add("米")      # レシピ側の「米」を同じクラスに入れる

    # rep_map: 各食材 -> 代表
    rep_map = {}
    for rep, members in eq_dict.items():
        for m in members:
            rep_map[m] = rep
    ingredients_set = set(model.Ingredients)
    # 代表 -> メンバー集合にまとめる
    class_members = {}
    for item, rep in rep_map.items():
        class_members.setdefault(rep, set()).add(item)

    new_rep_map = {}
    new_reps = []

    for rep, members in class_members.items():
        # このクラスの中で Ingredients に存在する名前を代表にする
        candidates = [m for m in members if m in ingredients_set]
        if candidates:
            new_rep = sorted(candidates)[0]   # 例: {"白米","ご飯","米"}∩Ingredients = {"ご飯","米"} → "ご飯"
        else:
            new_rep = rep                     # どれも無ければ元の代表を使う

        new_reps.append(new_rep)
        for m in members:
            new_rep_map[m] = new_rep

    rep_map = new_rep_map
    reps = sorted(set(new_reps))

    # 代表名ごとの y_item, item_used 変数を再定義
    # 1. 等価クラスに出てくる「食材名」の集合
    eq_items = set(rep_map.keys())  # itemequal_dict から作った rep_map 前提
    # 2. 代表名の集合
    reps = sorted(set(rep_map.values()))
    # 3. 等価クラスに出てこない食材（Ingredients 全体から引く）
    non_eq_items = sorted(set(model.Ingredients) - eq_items)
    
   # 等価クラスに属する食材は代表の y_item[rep] だけ数える
    term_eq = sum( model.y_item[rep] for rep in reps)

    # 等価クラスに属さない食材はそのままカウント
    term_non_eq = sum(model.y_item[i] for i in non_eq_items)

    # 種類数の項
    term_item = term_eq + term_non_eq


    # ご飯レシピの集合
    model.GohanRecipes = [r for r in model.Recipes if model.kind2_map[r] == 'ご飯']
    # ご飯以外のレシピ
    model.NonGohanRecipes = [r for r in model.Recipes if model.kind2_map[r] != 'ご飯']
    # ご飯レシピ → 7回まで
    def limit_gohan_rule(m, r):
        return sum(m.x[d, r] for d in m.Days) <= len(m.Days)
    model.LimitGohan = pyo.Constraint(model.GohanRecipes, rule=limit_gohan_rule)

    # ご飯以外レシピ → 1回まで
    def limit_non_gohan_rule(m, r):
        return sum(m.x[d, r] for d in m.Days) <= 1
    model.LimitNonGohan = pyo.Constraint(model.NonGohanRecipes, rule=limit_non_gohan_rule)

    # 目的関数
    # 重み
    weight_item     = 5    # 食材種類削減のメリット
    weight_regist   = 5    # 登録食材を使うメリット
    penalty_not_use = 15  # 登録食材を使わないペナルティ
    weight_multiple = 20   # 倍数ルールからのズレに体すえるペナルティ
    model.obj = pyo.Objective(
        expr = weight_item * term_item
            - weight_regist * sum(model.y_regist[i] for i in model.Ingredients)
            + penalty_not_use * sum(1 - model.y_regist[i] for i in model.Ingredients)
            + weight_multiple * sum(model.e[d,r,i] for d in model.Days for r in model.Recipes for i in model.Ingredients),
        sense = pyo.minimize
    )

    # --- Solver ---
    model.solver = pyo.SolverFactory('cbc')

    return model