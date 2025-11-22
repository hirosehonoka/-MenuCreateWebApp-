from flask import Flask,render_template,request,redirect,flash,url_for,session
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.ext.automap import automap_base
from sqlalchemy import  cast, BigInteger,literal,select,union_all,Column,DateTime,func,Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.mutable import MutableDict
from flask_login import UserMixin,LoginManager,login_user,login_required,logout_user,current_user
from werkzeug.security import generate_password_hash,check_password_hash
import os,json,requests,re,traceback
from collections import defaultdict
from dotenv import load_dotenv
from perplexity import Perplexity
from pyomo.environ import SolverFactory
import pyomo.environ as pyo
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP


app = Flask(__name__)

app.jinja_env.globals['getattr'] = getattr

load_dotenv() 

#ログイン管理システム
login_manager = LoginManager()
login_manager.init_app(app)

db = SQLAlchemy()

if app.debug:
    app.config["SECRET_KEY"] = os.urandom(24)
    DB_INFO = {
        'user':'postgres',
        'password':'',
        'host':'localhost',
        'name':'menuapp_db',
    }
    SQLALCHEMY_DATABASE_URI = 'postgresql+psycopg://{user}:{password}@{host}/{name}'.format(**DB_INFO)
else:
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY")
    SQLALCHEMY_DATABASE_URI = os.environ.get("DATABASE_URL").replace('postgresql://','postgresql+psycopg://')

app.config['SQLALCHEMY_DATABASE_URI'] = SQLALCHEMY_DATABASE_URI
db.init_app(app)

Base = automap_base()
with app.app_context():
    Base.prepare(db.engine, reflect=True)
RecipeUrl = Base.classes.recipeUrls
Menu = Base.classes.menu
ItemEqual = Base.classes.itemEquals
RecipeItem = Base.classes.recipeItems
RecipeNutrition = Base.classes.recipeNutritions
Recipe = Base .classes.recipes
ItemWeight = Base .classes .itemWeights
NutritionalTarget = Base .classes.nutritionalTargets
User = Base.classes.user

#現在のユーザを識別する
@login_manager.user_loader
def load_user(user_id):
    user = db.session.query(User).get(int(user_id))
    if user:
        return UserWrapper(user)
    return None

#プロンプト用テキストファイルを読み込む
def data2str(data):
    return str(data)

def generate_prompt(planning_data):
    base_dir = os.path.dirname(__file__)
    prompt_file_path = os.path.join(base_dir, "prompt_test4.txt")
    with open(prompt_file_path, encoding="utf-8") as f:
        prompt_template = f.read()
    # planning_dataをJSON化（整形・読みやすく）
    planning_json = json.dumps(planning_data, ensure_ascii=False, indent=2)
    # プレースホルダに安全に挿入
    prompt_ready = prompt_template.format(problem_data=planning_json)
    return prompt_ready

#MarkDown削除用
def extract_python_code(text):
    # Markdownコードブロック (```python ... ```
    m = re.search(r"```(?:python|パイソン)?\s*([\s\S]*?)```", text, re.DOTALL)
    if m:
        code_str = m.group(1)
    else:
        code_str = text
    return code_str.strip()

#辞書化関数・kind1の補完
def as_dict(row):
    out = {c.name: getattr(row, c.name) for c in row.__table__.columns}
    # # 万一欠損があれば補完
    # if 'kind1' not in out:
    #     out['kind1'] = ''
    return out

#ループ対策
def wrap_nutritional_target(nt):
    # nt.nutritionals, nt.userInfo が両方存在すると仮定
    nutr = nt.nutritionals if hasattr(nt, 'nutritionals') else nt.get('nutritionals', {})
    userinfo = nt.userInfo if hasattr(nt, 'userInfo') else nt.get('userInfo', {})
    # None補正
    for nut, val in nutr.items():
        if val is None:
            nutr[nut] = 0
    return {0: {"nutritionals": nutr, "userInfo": userinfo}}

#誤ったreturn文を自動削除処理
def sanitize_pyomo_code(code):
    # よくある誤りパターンを一括補正（False→Infeasible, True→Skip）
    code = code.replace('return False', 'return pyo.Constraint.Infeasible')
    code = code.replace('return True', 'return pyo.Constraint.Skip')
    return code

#ユーザーごとに制約とする栄養素を判断するためのフラグ
def should_use_pfc(userInfo):
    age = userInfo.get("年齢")
    sex = userInfo.get("性別")
    level = userInfo.get("運動レベル")

    # カロリー考慮で解なしになるユーザー群
    high_need_patterns = [
        ("18~29(歳)", "男性", "高い"),
        ("30~49(歳)", "男性", "高い"),
        ("50~64(歳)", "男性", "高い")
    ]

    if (age, sex, level) in high_need_patterns:
        return False  # PFC制約を外す
    return True

#日毎の献立
def extract_day_menus_with_categories(model, recipe_list):
    day_menus = {}
    for d in list(model.Days):
        menu = {}
        for cat in ['staple', 'main', 'soup', 'side']:
            if hasattr(model, cat):
                var = getattr(model, cat)
                selected_r = None
                max_val = -float('inf')
                for r in list(model.Recipes):
                    v = var[d, r].value
                    if v is not None and v > max_val:
                        max_val = v
                        selected_r = r
                # 0/1バイナリ判定（選ばれたレシピのみ記録）
                if max_val >= 0.5:
                    menu[cat] = selected_r  # recipeIdのみ格納
                else:
                    menu[cat] = None
        day_menus[f"menu{d}"] = menu
    return day_menus

def percent_to_g(percent, energy, factor):
    """%エネルギー→g換算"""
    try:
        return round(float(energy) * float(percent) / 100 / factor, 2)
    except Exception:
        return None

def sig_round(val, sig=4):
        if val is None:
            return None
        val = float(val)
        if val == 0:
            return 0
        digits = sig - int(Decimal(val).logb() + 1)
        return float(Decimal(val).scaleb(digits).to_integral_value(rounding=ROUND_HALF_UP).scaleb(-digits))

# class RecipeUrl(db.Model):
#     recipeId = db.Column(db.Integer,primary_key=True)
#     recipeTitle = db.Column(db.String,nullable=False)
#     recipeUrl = db.Column(db.String,nullable=True)
#     foodImageUrl= db.Column(db.String,nullable=False)

# class Menu(db.Model):
#     menuId = db.Column(db.Integer,primary_key=True)
#     menu1 = db.Column(JSONB,nullable=False)
#     menu2 = db.Column(JSONB,nullable=False)
#     menu3 = db.Column(JSONB,nullable=False)
#     menu4 = db.Column(JSONB,nullable=False)
#     menu5 = db.Column(JSONB,nullable=False)
#     menu6 = db.Column(JSONB,nullable=False)
#     menu7 = db.Column(JSONB,nullable=False)
#     userName = db.Column(db.String(20),nullable=False)
#     tokyo_timezone = pytz.timezone('Asia/Tokyo')
#     createdAt = db.Column(db.DateTime,nullable=False,default=datetime.now)

# class ItemEqual(db.Model):
#     itemName = db.Column(db.String,primary_key=True)
#     equals = db.Column(JSONB,nullable=False)

# class RecipeItem(db.Model):
#     recipeId = db.Column(db.Integer,primary_key=True)
#     items = db.Column(JSONB,nullable=False)

class UserWrapper(UserMixin):
    def __init__(self, user):
        self.user = user

    def __getattr__(self, name):
        return getattr(self.user, name)

    def get_id(self):
        # userIdカラムを文字列にして返す
        return str(self.user.userId)

    @property
    def is_active(self):
        return True

    @property
    def is_authenticated(self):
        return True

    @property
    def is_anonymous(self):
        return False
    
@app.route("/")
def home():
    return render_template("home.html", show_navbar=False)
    
@app.route("/showmenu")
@login_required
def show_menus():
    weekly_data = []
    menu = db.session.query(Menu).filter_by(userName=current_user.userName).first()

    if menu is None:
        return render_template("showmenu.html", weekly_data=[], show_navbar=True)
    
    menu_created_date = getattr(menu, 'createdAt', None)

    queries = []
    idx = 0

    for menu_col in ['menu1', 'menu2', 'menu3', 'menu4', 'menu5', 'menu6', 'menu7']:
        menu_json = getattr(menu, menu_col, {})
        for meal_type in ['staple', 'main', 'side', 'soup']:
            if isinstance(menu_json, dict):
                val = menu_json.get(meal_type)
            else:
                val = menu_json
            if val is None:  # nullはスキップ
                continue

            try:
                recipe_id = int(val) if not hasattr(val, 'astext') else int(val.astext)
            except (ValueError, TypeError):
                continue

            idx += 1

            query = (
                db.session.query(
                    RecipeUrl.recipeTitle.label('recipeTitle'),
                    RecipeUrl.recipeUrl.label('recipeUrl'),
                    RecipeUrl.foodImageUrl.label('foodImageUrl'),
                    literal(f'{menu_col}_{meal_type}').label('meal_type'),
                )
                .join(Menu, RecipeUrl.recipeId == cast(getattr(Menu, menu_col)[meal_type].astext, BigInteger))
                .filter(Menu.userName == current_user.userName)
                .filter(RecipeUrl.recipeId == recipe_id)
            )
            queries.append(query)

    if not queries:
        return render_template("showmenu.html", weekly_data=[], show_navbar=True)

    full_query = queries[0]
    for q in queries[1:]:
        full_query = full_query.union_all(q)

    core_queries = [q.statement for q in queries]
    full_union = union_all(*core_queries).alias('full_union')

    stmt = select(
        full_union.c.recipeTitle,
        full_union.c.recipeUrl,
        full_union.c.foodImageUrl,
        full_union.c.meal_type,
    )

    results = db.session.execute(stmt).fetchall()

    grouped = defaultdict(list)
    for r in results:
        menu_col = r.meal_type.split('_')[0] 
        grouped[menu_col].append(r)

    menu_order = ['menu1', 'menu2', 'menu3', 'menu4', 'menu5', 'menu6', 'menu7']
    weekly_data = [grouped[m] for m in menu_order]

    return render_template("showmenu.html", weekly_data=weekly_data, menu_created_date=menu_created_date, current_page='showmenu', show_navbar=True)

@app.route("/item")
@login_required
def show_item():
    aggregated_ingredients = {}
    menu = db.session.query(Menu).filter_by(userName=current_user.userName).first()
    if menu is None:
        return render_template("item.html", ingredients=aggregated_ingredients, total_types=0, current_page='item', show_navbar=True)

    # menu1〜menu7のすべてのrecipeIdを取得（中身が例えば {staple:123, main:456} のような構造を想定）
    recipe_ids = []
    for menu_col in ['menu1', 'menu2', 'menu3', 'menu4', 'menu5', 'menu6', 'menu7']:
        menu_json = getattr(menu, menu_col, {})
        for meal_type in ['staple', 'main', 'side', 'soup']:
            val = menu_json.get(meal_type)
            if val is None:
                continue
            try:
                recipe_id = int(val) if not hasattr(val, 'astext') else int(val.astext)
            except (ValueError, TypeError):
                continue
            recipe_ids.append(recipe_id)

    recipe_ids = list(set(recipe_ids))

    # ItemEqualの辞書作成: 等価食材名 -> 代表名
    item_equals = db.session.query(ItemEqual).all()
    item_equal_map = {}
    for eq in item_equals:
        equals_list = eq.equals.split(',') if eq.equals else []
        for k in equals_list:
            item_equal_map[k] = eq.itemName
        item_equal_map[eq.itemName] = eq.itemName

    # recipeIdごとにitemsを集計
    for rid in recipe_ids:
        recipe_item = db.session.query(RecipeItem).filter(RecipeItem.recipeId == rid).first()
        if not recipe_item:
            continue
        for ing_name, qty in recipe_item.items.items():
            # 代表名に変換
            rep_name = item_equal_map.get(ing_name, ing_name)
            # Noneなら0に変換
            qty = qty if qty is not None else 0
            # 加算
            aggregated_ingredients[rep_name] = aggregated_ingredients.get(rep_name, 0) + qty

    total_types = sum(1 for qty in aggregated_ingredients.values() if qty != 0)

    return render_template('item.html', ingredients=aggregated_ingredients, total_types=total_types, current_page='item', show_navbar=True)

@app.route('/registitem', methods=['GET'])
@login_required
def regist_item():
    return render_template('registitem.html',current_page='registitem',show_navbar=True)

@app.route('/createmenu', methods=['GET','POST'])
@login_required
def create_menu():
    if request.method == 'POST':
        # リクエストデータを受け取る
        regist_item = request.json if request.json else {}

        # 0. ログインユーザが以前取得した献立を削除
        menu = db.session.query(Menu).filter_by(userName=current_user.userName).first()
        if menu is not None:
            db.session.delete(menu)
            db.session.commit()

        # 1. ログインユーザ情報取得とユーザの以前の献立削除
        user = db.session.query(User).filter_by(userName=current_user.userName).first()
        if not user or not user.userInfo:
            flash("ユーザーターゲットが登録されていません")
            return redirect(url_for('index'))
        user_userInfo = user.userInfo # jsonb型。Pythonではdict想定
        
        # 2. NutritionalTargetからマッチングレコード探索
        # 年齢、性別、運動レベル
        userInfo_conditions = user_userInfo.copy()
        age = userInfo_conditions.get('年齢', None)
        gender = userInfo_conditions.get('性別', None)
        activity = userInfo_conditions.get('運動レベル', None)

        # 「75歳以上」かつ「運動レベル 高い」なら運動レベルを「ふつう」に
        activity_query = activity
        if age and ('75' in age and activity == '高い'):
            activity_query = 'ふつう'

        # SQLAlchemyによるjsonbフィールド完全一致 AND 年齢・性別・運動レベル条件
        nt = db.session.query(NutritionalTarget).filter(
            NutritionalTarget.userInfo['年齢'].astext == str(age),
            NutritionalTarget.userInfo['性別'].astext == str(gender),
            NutritionalTarget.userInfo['運動レベル'].astext == str(activity_query)
        ).first()
        if nt is None:
            flash("栄養ターゲットが見つかりません")
            return redirect(url_for('index'))
        nutritional = nt.nutritionals

        # 3. レシピ・食材・関連データ一式をIDごとにまとめて取得
        recipes = db.session.query(Recipe).all()
        recipe_nutritions = {r.recipeId: r.nutritions for r in db.session.query(RecipeNutrition).all()}
        recipe_items = {ri.recipeId: ri.items for ri in db.session.query(RecipeItem).all()}
        item_weights = {iw.itemName: iw.weights for iw in db.session.query(ItemWeight).all()}
        item_equals = {ie.itemName: ie.equals for ie in db.session.query(ItemEqual).all()}

        # PFCを考慮するかどうか判断する
        no_pfc_users = [
            ("18~29(歳)", "男性", "高い"),
            ("30~49(歳)", "男性", "高い"),
            ("50~64(歳)", "男性", "高い"),
        ]

        if user_userInfo in no_pfc_users:
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

        target_keys = list(nutrition_match.keys())

        filtered_nutritional = {
            nutrition_match[k]: v
            for k, v in nt.nutritionals.items()
            if k in nutrition_match and nutrition_match[k] is not None
        }

        nutri_keys = list(filtered_nutritional.keys())
        filtered_recipe_nutritions = {
            r.recipeId: {k: v for k, v in r.nutritions.items() if k in nutri_keys}
            for r in db.session.query(RecipeNutrition).all()
        }

        # SQLAlchemyからリストやディクショナリでデータ取得
        recipe_dict = {}
        for r in db.session.query(Recipe).all():
            d = as_dict(r)
            recipe_dict[r.recipeId] = d
        itemweight_dict = {}
        for iw in db.session.query(ItemWeight).all():
            d = as_dict(iw)  # itemName, weights しか存在しない場合
            # "weights"を必ずリスト化
            if not isinstance(d.get('weights'), list):
                d['weights'] = [d['weights']] if d.get('weights') is not None else []
            # "kind1"がない場合は空文字で補完
            d['kind1'] = ''
            itemweight_dict[d['itemName']] = d
        itemequal_dict = {ie.itemName: as_dict(ie) for ie in db.session.query(ItemEqual).all()}
        recipeitem_dict = {}
        recipeitem_list = [as_dict(ri) for ri in db.session.query(RecipeItem).all()]
        for rec in recipeitem_list:
            rid = rec['recipeId']
            items = rec.get('items', {})
            items_fixed = {k: (v if v is not None else 0) for k, v in items.items()}  # ←qtyそのまま
            recipeitem_dict[rid] = items_fixed
        recipenutrition_dict = {rn.recipeId: as_dict(rn) for rn in db.session.query(RecipeNutrition).all()}
        nutritionaltarget_dict = wrap_nutritional_target(nt)
        for nt_id, nt_val in nutritionaltarget_dict.items():
            if 'nutritionals' in nt_val:
                for nut, val in nt_val['nutritionals'].items():
                    if val is None:
                        nt_val['nutritionals'][nut] = 0
        user = db.session.query(User).filter_by(userName=current_user.userName).first()
        menstruation = user.menstruation

        days = list(range(1,8)) 
        recipe_ids = list(recipe_dict.keys())

        # for r in recipe_dict:
        #     print(r, type(recipe_dict[r]), recipe_dict[r])

        # print("days:", days)
        # print("レシピIDリスト:", recipe_ids)
        # print("itemweight_dict:", itemweight_dict)
        # print("itemequal_dict:", itemequal_dict)
        # print("recipeitem_dict keys:", recipeitem_dict.keys())
        # print("nutritionaltarget_dict", nutritionaltarget_dict)
        # print("filtered_recipe_nutritions:", filtered_recipe_nutritions)
        # print("filtered_recipe_nutritions keys:", filtered_recipe_nutritions.keys())
        # print("regist_item", regist_item)

        # # 4. プロンプト文生成
        # # データのまとめ
        # planning_data = {
        #     "days": days,                                    # 例: list(range(1,8))
        #     "recipe_dict": recipe_dict,                          
        #     "recipe_ids": recipe_ids,                           # レシピIDのリスト
        #     "recipeitem_dict": recipeitem_dict,              # {recipe_id: {item_name: qty}, ...}
        #     "filtered_recipe_nutritions": filtered_recipe_nutritions,  # {recipe_id: {nutrient_name: value}, ...}
        #     "nutritionaltarget_dict": nutritionaltarget_dict,    # {ターゲットID: ...}
        #     "itemweight_dict": itemweight_dict,              # {item_name: weights}
        #     "itemequal_dict": itemequal_dict,                # {item_name: equals_list}
        #     "userInfo": userInfo,
        #     "regist_item": regist_item
        # }
        # regist_item = session.get('regist_item')
        # solution_prompt = generate_prompt(planning_data)
        # print('プロンプト：')
        # print(solution_prompt)

        # optimization_input = client.chat.completions.create(
        #     messages=[{"role": "user", "content": solution_prompt}],
        #     model="sonar",
        #     temperature=0.1
        # )

        # # 6. Pyomo（+cbc）最適化
        # pyomo_code_str_raw = optimization_input.choices[0].message.content
        # pyomo_code_str = extract_python_code(pyomo_code_str_raw)
        # print('API出力内容:')
        # print(pyomo_code_str)
        # print('API出力完了')

        #API出力コードの読み込み(ソルバー周辺調整用)
        base_dir = os.path.dirname(__file__)  # menuapp.pyのある場所
        api_file_path = os.path.join(base_dir, "api_pyomo_model4.py")
        with open(api_file_path, encoding='utf-8') as f:
            pyomo_code_str = f.read()
        
        pyomo_code_str = sanitize_pyomo_code(pyomo_code_str)
        use_pfc = should_use_pfc(user_userInfo)

        scope = {
            'days': days,                                    # list(range(1,8)) 等
            'recipe_dict': recipe_dict,
            'recipe_ids': recipe_ids,             # レシピIDリスト
            'recipeitem_dict': recipeitem_dict,                   # {rid: {item: qty}}
            'filtered_recipe_nutritions': filtered_recipe_nutritions,   # {rid: {nutrient: value}}
            'nutritionaltarget_dict': nutritionaltarget_dict,     # {ターゲットID: {...}}等
            'itemweight_dict': itemweight_dict,                   # {item: weights}
            'itemequal_dict': itemequal_dict,                     # {item: equals}
            'menstruation': menstruation,                        
            'regist_item': regist_item,                      # dict or None
            'use_pfc': use_pfc
        }

        exec(pyomo_code_str, scope, scope)
        build_model = scope.get('build_model')

        print("use_pfc:", use_pfc)
        print("cal_val:", next(iter(nutritionaltarget_dict.values()))['nutritionals'].get('カロリー'))
        print("sample nutritionals:", next(iter(nutritionaltarget_dict.values()))['nutritionals'])
        # PFCキーが filtered_recipe_nutritions に存在するか確認（最初の数レシピで確認）
        for k in ['カロリー(kcal)','たんぱく質(g)','脂質(g)','炭水化物(g)']:
            sample_has = any(k in v for v in filtered_recipe_nutritions.values())
            print(f"nut key {k} present in recipes?:", sample_has)

        model = build_model(
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
            use_pfc
        )

        # 必要ならnutri_keysも抽出
        nutri_keys = set()
        for nutr_t in nutritionaltarget_dict.values():
            nutri_keys.update(nutr_t.get('nutritionals', {}).keys())
        nutri_keys = list(nutri_keys)

        print('ソルバー準備完了')

        cbc_path = "/Users/hiruse/cbc/bin/cbc"
        solver = SolverFactory('cbc', executable=cbc_path)  # フルパスを指定
        solver.options['sec'] = 300          # 最大5分実行
        solver.options['ratioGap'] = 0.02    # 2%以内で打ち切り
        results = solver.solve(model,tee=True)
        day_menus = extract_day_menus_with_categories(model,list(recipe_dict.values()))
        print('献立作成完了')

        import logging
        from pyomo.util.infeasible import log_infeasible_constraints

        logging.getLogger('pyomo.core').setLevel(logging.INFO)
        log_infeasible_constraints(model)

        # 7. 曜日ごとのMenuレコード保存
        day_menus = {}
        for d in model.Days:
            menu_name = f"menu{d}"
            day_menus[menu_name] = {}
            
            for r in model.Recipes:
                val = pyo.value(model.x[d, r])
                if val is not None and val > 0.5:
                    kind1 = model.kind1_map[r]  # 'staple', 'main', 'side', 'soup'
                    # 1日1品しか選ばれない前提で key:value 形式に格納
                    day_menus[menu_name][kind1] = r

        menu_obj = Menu(
            userName=current_user.userName,
            menu1=day_menus.get('menu1', {}),
            menu2=day_menus.get('menu2', {}),
            menu3=day_menus.get('menu3', {}),
            menu4=day_menus.get('menu4', {}),
            menu5=day_menus.get('menu5', {}),
            menu6=day_menus.get('menu6', {}),
            menu7=day_menus.get('menu7', {}),
            createdAt=datetime.now()
        )

        db.session.add(menu_obj)
        db.session.commit()
        print('menucreate終了')

        # # デバッグ出力
        # print("=== day_menus ===")
        # print(day_menus)

        return redirect('/showmenu')

@app.route("/nutrition")
@login_required
def show_nutrition():
    # NutritionalTarget→RecipeNutritionへのkey変換dict
    key_map = {
        "カロリー":"カロリー(kcal)",
        "たんぱく質_上限":"たんぱく質(g)",
        "たんぱく質_下限":"たんぱく質(g)",
        "脂質_上限":"脂質(g)",
        "脂質_下限":"脂質(g)",
        "炭水化物_上限":"炭水化物(g)",
        "炭水化物_下限":"炭水化物(g)",
        "食塩_上限":"食塩(g)",
        "食物繊維_下限":"食物繊維(g)",
        "カルシウム_上限":"カルシウム(mg)",
        "カルシウム_下限":"カルシウム(mg)",
        "ビタミンA_上限":"ビタミンA(μg)",
        "ビタミンA_下限":"ビタミンA(μg)",
        "ビタミンD_上限":"ビタミンD(μg)",
        "ビタミンD_下限":"ビタミンD(μg)",
        "ビタミンC_下限":"ビタミンC(mg)",
        "ビタミンB1_下限":"ビタミンB₁(mg)",
        "ビタミンB2_下限":"ビタミンB₂(mg)",
        "鉄_下限":"鉄(mg)",
        "鉄・月経時_下限":"鉄(mg)"
    }
    aggregated_nutrition = {}
    recipe_ids = []

    menu = db.session.query(Menu).filter_by(userName=current_user.userName).first()
    if menu is None:
        return render_template("nutrition.html", nutrition=aggregated_nutrition, nutritionals={}, current_page='nutrition', show_navbar=True)

    # menu1〜menu7からrecipeId収集
    for menu_col in ['menu1','menu2','menu3','menu4','menu5','menu6','menu7']:
        menu_json = getattr(menu, menu_col, {})
        for meal_type in ['staple','main','side','soup']:
            recipe_id = menu_json.get(meal_type)
            if recipe_id is None:
                continue
            try:
                recipe_id = int(recipe_id) if not hasattr(recipe_id, 'astext') else int(recipe_id.astext)
            except (ValueError, TypeError):
                continue
            recipe_ids.append(recipe_id)

            rec_nut = db.session.query(RecipeNutrition).filter(RecipeNutrition.recipeId == recipe_id).first()
            if not rec_nut:
                continue

            for nut_name, nut_val in rec_nut.nutritions.items():
                nut_val = nut_val if nut_val is not None else 0
                aggregated_nutrition[nut_name] = aggregated_nutrition.get(nut_name, 0) + nut_val

    recipe_ids = list(set(recipe_ids))
    
    rounded_nutrition = {k: sig_round(v, 4) for k, v in aggregated_nutrition.items()}

    # ユーザー目標値取得・対応keyにリネーム
    user = db.session.query(User).filter_by(userName=current_user.userName).first()
    nutritionals_raw = {}
    if user is not None:
        nutritional_obj = db.session.query(NutritionalTarget).filter(NutritionalTarget.userInfo == user.userInfo).first()
        if nutritional_obj is not None and nutritional_obj.nutritionals:
            # menstruationが'あり'なら鉄・月経時_下限、それ以外は鉄_下限
            nutritionals_raw = nutritional_obj.nutritionals.copy()
            if user.menstruation == 'あり':
                # 鉄_下限を鉄・月経時_下限に置き換える
                if "鉄・月経時_下限" in nutritional_obj.nutritionals:
                    nutritionals_raw["鉄_下限"] = nutritional_obj.nutritionals["鉄・月経時_下限"]
            else:
                # 月経なし → 「鉄・月経時_下限」を削除
                if "鉄・月経時_下限" in nutritionals_raw:
                    nutritionals_raw.pop("鉄・月経時_下限")


    # 各栄養素ごとに下限・上限（key_mapによる統一名）で {item:{'min':val, 'max':val}}に再構成
    nutritionals = {}

    # カロリー特別処理（targetsの"カロリー"キーで±10％幅）
    cal_base = nutritionals_raw.get("カロリー")
    if cal_base is not None:
        cal_min = int(cal_base * 0.9)
        cal_max = int(cal_base * 1.1)
        nutritionals["カロリー(kcal)"] = {"min": cal_min, "max": cal_max}

    for k, v in nutritionals_raw.items():
        rep_key = key_map.get(k)
        if not rep_key:
            continue
        if '_下限' in k:
            if rep_key in {"たんぱく質(g)", "炭水化物(g)"}:
                nutritionals.setdefault(rep_key, {})['min'] = percent_to_g(v, cal_base, 4)
            elif rep_key in {"脂質(g)"}:
                nutritionals.setdefault(rep_key, {})['min'] = percent_to_g(v, cal_base, 9)
            else:
                nutritionals.setdefault(rep_key, {})['min'] = v
        elif '_上限' in k:
            if rep_key in {"たんぱく質(g)", "炭水化物(g)"}:
                nutritionals.setdefault(rep_key, {})['max'] = percent_to_g(v, cal_base, 4)
            elif rep_key in {"脂質(g)"}:
                nutritionals.setdefault(rep_key, {})['max'] = percent_to_g(v, cal_base, 9)
            else:
                nutritionals.setdefault(rep_key, {})['max'] = v

    return render_template("nutrition.html", nutrition=rounded_nutrition, nutritionals=nutritionals, current_page='nutrition', show_navbar=True)


@app.route("/signup",methods=['GET','POST'])
def signup():
    if request.method == 'POST':
        userName = request.form.get('userName')
        password = request.form.get('password')
        hashed_pass = generate_password_hash(password)
        userAge = request.form.get('userAge')
        userGender = request.form.get('userGender')
        userExerciseLevel = request.form.get('userExerciseLevel')
        menstruation = request.form.get('menstruation')
        userInfo ={"年齢":userAge,"性別":userGender,"運動レベル":userExerciseLevel}
        user = User(userName=userName,password=hashed_pass,userInfo=userInfo,menstruation=menstruation)
        db.session.add(user)
        db.session.commit()
        return redirect('/login')
    elif request.method == 'GET':
        return render_template('signup.html', show_navbar=False)
    
@app.route("/userupdate", methods=['GET', 'POST'])
@login_required
def user_update():
    user = db.session.query(User).filter_by(userId=int(current_user.get_id())).first()

    if request.method == 'POST':
        userAge = request.form.get('userAge')
        userExerciseLevel = request.form.get('userExerciseLevel')
        menstruation = request.form.get('menstruation', 'なし')

        if user and user.userInfo:
            # userInfoは辞書と仮定
            info = dict(user.userInfo)
            info['年齢'] = userAge
            info['運動レベル'] = userExerciseLevel
            user.userInfo = info

            # menstruation カラムに代入
            user.menstruation = menstruation

            db.session.commit()
        return redirect('/showmenu')

    else:  # GET
        userGender_selected = user.userInfo.get('性別') if user and user.userInfo else None
        userAge_selected = user.userInfo.get('年齢') if user and user.userInfo else None
        userExerciseLevel_selected = user.userInfo.get('運動レベル') if user and user.userInfo else None
        menstruation_selected = user.menstruation if user else 'なし'

        return render_template(
            'userupdate.html',
            userGender_selected=userGender_selected,
            userAge_selected=userAge_selected,
            userExerciseLevel_selected=userExerciseLevel_selected,
            menstruation_selected=menstruation_selected,
            current_page='userupdate',
            show_navbar=True
        )

@app.route('/login',methods=['GET','POST'])
def login():
    if request.method == 'POST':
        userName = request.form.get('userName')
        password = request.form.get('password')
        user = db.session.query(User).filter_by(userName=userName).first()
        if check_password_hash(user.password,password=password):
            wrapped_user = UserWrapper(user)
            login_user(wrapped_user)
            return redirect('/showmenu')
        else:
            flash('ユーザ名かパスワードが違います')
            return redirect(url_for('login'))
    elif request.method == 'GET':
        return render_template('login.html', show_navbar=False)
    
@app.route('/logout',methods=['GET','POST'])
@login_required
def logout():
    logout_user()
    return redirect('/')


#Flask --app source.main.menuapp run --debug で実行