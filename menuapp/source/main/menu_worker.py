import os
import time
import json
import logging
from sqlalchemy import text
from pyomo.util.infeasible import log_infeasible_constraints
from datetime import datetime, timezone, timedelta
from source.main.menuapp import (
    db, CBC_PATH, User, Menu, Recipe, RecipeItem, RecipeNutrition,
    ItemWeight, ItemEqual, NutritionalTarget
)
from pyomo.environ import SolverFactory
from source.main.menuapp import app, db, sanitize_pyomo_code, should_use_pfc, wrap_nutritional_target

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

def as_dict(obj):
    """SQLAlchemyオブジェクトを辞書に変換"""
    return {c.name: getattr(obj, c.name) for c in obj.__table__.columns}

def load_reference_data():
    with app.app_context():
        recipe_dict = {r.recipeId: as_dict(r) for r in db.session.query(Recipe).all()}

        itemweight_dict = {}
        for iw in db.session.query(ItemWeight).all():
            d = as_dict(iw)
            d['weights'] = d.get('weights') if isinstance(d.get('weights'), list) else [d['weights']] if d.get('weights') else []
            d['kind1'] = d.get('kind1', '')
            itemweight_dict[d['itemName']] = d

        itemequal_dict = {ie.itemName: as_dict(ie) for ie in db.session.query(ItemEqual).all()}

        recipeitem_dict = {}
        for ri in db.session.query(RecipeItem).all():
            rec = as_dict(ri)
            rid = rec['recipeId']
            items_fixed = {k: (v if v is not None else 0) for k, v in rec.get('items', {}).items()}
            recipeitem_dict[rid] = items_fixed

        recipe_nutrition_dict = {
            r.recipeId: r.nutritions for r in db.session.query(RecipeNutrition).all()
        }

    return recipe_dict, itemweight_dict, itemequal_dict, recipeitem_dict, recipe_nutrition_dict

def main_worker_loop():
    with app.app_context():
        # 参照データロード
        RECIPE_DICT, ITEMWEIGHT_DICT, ITEMEQUAL_DICT, RECIPEITEM_DICT, RECIPE_NUTRITION_DICT = load_reference_data()
        
        while True:
            try:
                jobs = db.session.execute(text(
                    "SELECT * FROM menu_jobs WHERE status='pending' ORDER BY created_at"
                )).fetchall()

                for job in jobs:
                    solver_duration = None
                    db_duration = None
                    day_menus = {}
                    try:
                        # ジョブを running に更新
                        db.session.execute(text(
                            "UPDATE menu_jobs SET status='running', updated_at=NOW() WHERE id=:id"),
                            {'id': job.id}
                        )
                        db.session.commit()

                        # ユーザー取得
                        user = db.session.query(User).filter_by(userName=job.userName).first()
                        if not user:
                            logging.error(f"User {job.userName} not found")
                            db.session.execute(text(
                                "UPDATE menu_jobs SET status='failed', updated_at=NOW() WHERE id=:id"),
                                {'id': job.id}
                            )
                            db.session.commit()
                            continue

                        user_info = user.userInfo
                        regist_item = json.loads(job.regist_item) if job.regist_item else {}

                        # NutritionalTarget 取得
                        age = user_info.get('年齢')
                        gender = user_info.get('性別')
                        activity = user_info.get('運動レベル')
                        activity_query = 'ふつう' if age and '75' in age and activity == '高い' else activity

                        nt = db.session.query(NutritionalTarget).filter(
                            NutritionalTarget.userInfo['年齢'].astext == str(age),
                            NutritionalTarget.userInfo['性別'].astext == str(gender),
                            NutritionalTarget.userInfo['運動レベル'].astext == str(activity_query)
                        ).first()

                        if nt is None:
                            logging.error(f"NutritionalTarget not found for user {job.userName}")
                            db.session.execute(text(
                                "UPDATE menu_jobs SET status='failed', updated_at=NOW() WHERE id=:id"),
                                {'id': job.id}
                            )
                            db.session.commit()
                            continue

                        # PFC判定
                        no_pfc_users = [
                            ("18~29(歳)", "男性", "高い"),
                            ("30~49(歳)", "男性", "高い"),
                            ("50~64(歳)", "男性", "高い"),
                        ]
                        # age, gender, activity はユーザー情報から取得済み
                        if (age, gender, activity) in no_pfc_users:
                            # PFC無視ユーザー向け
                            nutrition_match = {
                                "食塩_上限":"食塩(g)",
                                "食物繊維_下限":"食物繊維(g)",
                                "カルシウム_上限":"カルシウム(mg)",
                                "カルシウム_下限":"カルシウム(mg)",
                                "ビタミンA_上限":"ビタミンA(μg)",
                                "ビタミンA_下限":"ビタミンA(μg)",
                                "ビタミンD_上限": "ビタミンD(μg)",
                                "ビタミンD_下限": "ビタミンD(μg)",
                                "ビタミンC_下限": "ビタミンC(mg)",
                                "ビタミンB1_下限":"ビタミンB₁(mg)",
                                "ビタミンB2_下限":"ビタミンB₂(mg)",
                                "鉄・月経時_下限":"鉄(mg)",
                                "鉄_下限":"鉄(mg)"
                            }
                        else:
                            # 通常ユーザー向け（PFC制約あり）
                            nutrition_match = {
                                "カロリー":"カロリー(kcal)",
                                "たんぱく質_上限":"たんぱく質(g)",
                                "たんぱく質_下限":"たんぱく質(g)",
                                "脂質_上限":"脂質(g)",
                                "脂質_下限":"脂質(g)",
                                "炭水化物_上限":"炭水化物(g)",
                                "炭水化物_下限":"炭水化物(g)",
                                "食物繊維_下限":"食物繊維(g)",
                                "カルシウム_上限":"カルシウム(mg)",
                                "カルシウム_下限":"カルシウム(mg)",
                                "ビタミンA_上限":"ビタミンA(μg)",
                                "ビタミンA_下限":"ビタミンA(μg)",
                                "ビタミンD_上限": "ビタミンD(μg)",
                                "ビタミンD_下限": "ビタミンD(μg)",
                                "ビタミンC_下限": "ビタミンC(mg)",
                                "ビタミンB1_下限":"ビタミンB₁(mg)",
                                "ビタミンB2_下限":"ビタミンB₂(mg)",
                                "鉄・月経時_下限":"鉄(mg)",
                                "鉄_下限":"鉄(mg)"
                            }

                        filtered_nutritional = {
                            nutrition_match[k]: v
                            for k, v in nt.nutritionals.items()
                            if k in nutrition_match and nutrition_match[k] is not None
                        }

                        # wrap
                        nutritionaltarget_dict = wrap_nutritional_target(nt)
                        for nt_id, nt_val in nutritionaltarget_dict.items():
                            if 'nutritionals' in nt_val:
                                for nut, val in nt_val['nutritionals'].items():
                                    if val is None:
                                        nt_val['nutritionals'][nut] = 0

                        menstruation = user.menstruation
                        days = list(range(1, 8))
                        recipe_ids = list(RECIPE_DICT.keys())

                        # Pyomo モデル読み込み
                        base_dir = os.path.dirname(__file__)
                        api_file_path = os.path.join(base_dir, "api_pyomo_model.py")
                        with open(api_file_path, encoding='utf-8') as f:
                            pyomo_code_str = f.read()
                        pyomo_code_str = sanitize_pyomo_code(pyomo_code_str)
                        use_pfc = should_use_pfc(user_info)

                        scope = {
                            'days': days,
                            'recipe_dict': RECIPE_DICT,
                            'recipe_ids': recipe_ids,
                            'recipeitem_dict': RECIPEITEM_DICT,
                            'filtered_recipe_nutritions': RECIPE_NUTRITION_DICT,
                            'nutritionaltarget_dict': nutritionaltarget_dict,
                            'itemweight_dict': ITEMWEIGHT_DICT,
                            'itemequal_dict': ITEMEQUAL_DICT,
                            'menstruation': menstruation,
                            'regist_item': regist_item,
                            'use_pfc': use_pfc
                        }

                        exec(pyomo_code_str, scope, scope)
                        build_model = scope.get('build_model')
                        model = build_model(
                            days, RECIPE_DICT, recipe_ids, RECIPEITEM_DICT, RECIPE_NUTRITION_DICT,
                            nutritionaltarget_dict, ITEMWEIGHT_DICT, ITEMEQUAL_DICT,
                            menstruation, regist_item, use_pfc
                        )

                        # Solver 実行
                        solver = SolverFactory('cbc', executable=CBC_PATH)
                        solver.options['sec'] = 20
                        solver.options['ratioGap'] = 0.02
                        solver_start = time.time()
                        try:
                            result = solver.solve(model, tee=False)
                            logging.info("Solver finished successfully")
                        except Exception as e:
                            log_infeasible_constraints(model)
                            logging.error(f"Solver failed: {e}")
                        solver_end = time.time()
                        solver_duration = solver_end - solver_start

                        # メニュー保存
                        day_menus = {}
                        for d in model.Days:
                            menu_name = f"menu{d}"
                            day_menus[menu_name] = {}
                            for r in model.Recipes:
                                var = model.x[d, r]
                                if var.value is not None and var.value > 0.5:
                                    kind1 = model.kind1_map[r]
                                    day_menus[menu_name][kind1] = r

                        # DB保存
                        # JST (UTC+9) に変換
                        now_jst = datetime.now(timezone.utc) + timedelta(hours=9)
                        db_start = time.time()
                        menu_obj = Menu(
                            userName=job.userName,
                            menu1=day_menus.get('menu1', {}),
                            menu2=day_menus.get('menu2', {}),
                            menu3=day_menus.get('menu3', {}),
                            menu4=day_menus.get('menu4', {}),
                            menu5=day_menus.get('menu5', {}),
                            menu6=day_menus.get('menu6', {}),
                            menu7=day_menus.get('menu7', {}),
                            createdAt=now_jst.replace(tzinfo=None)
                        )
                        db.session.add(menu_obj)
                        db.session.commit()
                        db_end = time.time()
                        db_duration = db_end - db_start

                        # 成功ログ
                        logging.info(json.dumps({
                            "user": job.userName,
                            "status": "成功",
                            "solver_duration": solver_duration,
                            "db_duration": db_duration,
                            "day_menus": day_menus,
                            "regist_item": regist_item,
                            "error_type": None,
                            "error_trace": None,
                            "timestamp": datetime.now().isoformat()
                        }, ensure_ascii=False))

                        # ジョブ完了
                        db.session.execute(text(
                            "UPDATE menu_jobs SET status='done', result_json=:result, updated_at=NOW() WHERE id=:id"),
                            {'result': json.dumps(day_menus, ensure_ascii=False), 'id': job.id}
                        )
                        db.session.commit()
                    except Exception as e:
                        # 失敗ログ
                        logging.error(json.dumps({
                            "user": getattr(user, 'userName', 'Unknown'),
                            "status": "失敗",
                            "solver_duration": solver_duration,
                            "db_duration": db_duration,
                            "day_menus": day_menus,
                            "regist_item": regist_item,
                            "error_type": type(e).__name__,
                            "error_trace": str(e),
                            "timestamp": datetime.now().isoformat()
                        }, ensure_ascii=False))
                        db.session.execute(text(
                            "UPDATE menu_jobs SET status='failed', updated_at=NOW() WHERE id=:id"),
                            {'id': job.id}
                        )
                        db.session.commit()
                    pass

            except Exception as e:
                logging.error(f"Worker loop error: {e}")

            time.sleep(5)


if __name__ == "__main__":
    main_worker_loop()