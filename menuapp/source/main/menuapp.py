from flask import Flask,render_template,request,redirect,flash,url_for, send_file,session
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.ext.automap import automap_base
from sqlalchemy import  cast, BigInteger,literal,select,union_all,text
from flask_login import UserMixin,LoginManager,login_user,login_required,logout_user,current_user
from werkzeug.security import generate_password_hash,check_password_hash
import os,json,re,logging,traceback,time
from collections import defaultdict
from dotenv import load_dotenv
from pyomo.environ import SolverFactory
import pyomo.environ as pyo
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pyomo.opt import TerminationCondition
import pandas as pd
import itertools
from itertools import product
from pyomo.util.infeasible import log_infeasible_constraints
from pyomo.opt import TerminationCondition
from sqlalchemy.exc import OperationalError, SQLAlchemyError


app = Flask(__name__)

app.jinja_env.globals['getattr'] = getattr

load_dotenv() 

#ログイン管理システム
login_manager = LoginManager()
login_manager.init_app(app)

log_file_path = os.path.join(os.path.dirname(__file__), "../../menu_app.log")
# ログ設定
logger = logging.getLogger()
logger.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
logging.getLogger("pyomo").setLevel(logging.ERROR)
logging.getLogger('pyomo.core').setLevel(logging.WARNING)
logging.getLogger("pyomo.solvers").setLevel(logging.ERROR)
logging.getLogger("pyomo.opt").setLevel(logging.ERROR)

# ファイルハンドラ（追記）
file_handler = logging.FileHandler(log_file_path, mode='a', encoding='utf-8')
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

# 標準出力用ハンドラ
stream_handler = logging.StreamHandler()
stream_handler.setFormatter(formatter)
logger.addHandler(stream_handler)

db = SQLAlchemy()

if app.debug:
    app.config["SECRET_KEY"] = os.urandom(24)
    DB_INFO = {
        'user':'postgres',
        'password':'',
        'host':'localhost',
        'name':'postgres',
    }
    SQLALCHEMY_DATABASE_URI = 'postgresql+psycopg://{user}:{password}@{host}/{name}'.format(**DB_INFO)
    CBC_PATH = "/Users/hiruse/cbc/bin/cbc"
else:
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY")
    SQLALCHEMY_DATABASE_URI = os.environ.get("DATABASE_URL").replace('postgres://','postgresql+psycopg2://')
    CBC_PATH = "/app/bin/cbc"

app.config['SQLALCHEMY_DATABASE_URI'] = SQLALCHEMY_DATABASE_URI
db.init_app(app)
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    "pool_size": 5,       # 同時に維持する接続数
    "max_overflow": 10,   # pool_sizeを超えて一時的に作る接続数
    "pool_timeout": 30,   # 接続取得で待つ秒数
    "pool_recycle": 1800  # 既存接続の再利用までの秒数（30分）
}

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

# エラーの分類
def classify_error_jp(e=None, solver_result=None):
    # --- ① Pyomoの結果から判定 ---
    if solver_result is not None:
        try:
            term = solver_result.solver.termination_condition

            if term == TerminationCondition.infeasible:
                return "制約未達のため解なし"
            elif term == TerminationCondition.maxTimeLimit:
                return "ソルバー時間制限超過"
            elif term == TerminationCondition.unbounded:
                return "解が発散（Unbounded）"
        except:
            pass

    # --- ② 例外の型で判定 ---
    if isinstance(e, OperationalError):
        return "データベース接続エラー"
    if isinstance(e, SQLAlchemyError):
        return "データベース内部エラー"
    if isinstance(e, MemoryError):
        return "サーバーメモリ不足"

    # --- ③ メッセージ文字列で判定 ---
    if e:
        msg = str(e).lower()

        if "infeasible" in msg:
            return "制約未達のため解なし"
        elif "timeout" in msg or "time limit" in msg:
            return "ソルバー時間制限超過"
        elif "lock" in msg or "deadlock" in msg:
            return "データベースロック競合"
        elif "too many" in msg or "overloaded" in msg:
            return "サーバー高負荷"
        elif "memory" in msg:
            return "サーバーメモリ不足"
        elif "solver" in msg:
            return "ソルバー内部エラー"
        elif "connection" in msg or "database" in msg:
            return "データベース接続エラー"

    return "不明なエラー"


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
    return render_template('createmenu.html',current_page='createmenu',show_navbar=True)

@app.route("/menu_status")
@login_required
def menu_status():
    menu = db.session.query(Menu).filter_by(userName=current_user.userName).order_by(Menu.createdAt.desc()).first()
    if menu:
        return {"status": "done"}
    else:
        return {"status": "pending"}

# 非同期化
@app.route('/createmenu', methods=['GET','POST'])
@login_required
def create_menu_async():
    try:
        regist_item = request.json if request.json else {}

        # ログインユーザが以前取得した献立を削除
        existing_menu = db.session.query(Menu).filter_by(userName=current_user.userName).first()
        if existing_menu:
            db.session.delete(existing_menu)
            db.session.commit()

        # 既存ジョブ削除（同ユーザーが連続で作成した場合）
        db.session.execute(text(
            "DELETE FROM menu_jobs WHERE userName=:userName AND status IN ('pending','running')"),
            {'userName': current_user.userName}
        )
        db.session.commit()

        # ジョブ登録
        db.session.execute(text(
            "INSERT INTO menu_jobs (userName, regist_item) VALUES (:userName, :regist_item)"),
            {'userName': current_user.userName, 'regist_item': json.dumps(regist_item)}
        )
        db.session.commit()

        return {"status": "queued", "message": "献立作成をキューに登録しました。完了までお待ちください。"}, 202

    except Exception as e:
        logging.error(f"Failed to queue job: {e}")
        return {"status": "error", "message": "ジョブ登録に失敗しました"}, 500

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

        # 空欄チェック
        if not userName or not password:
            flash("ユーザー名とパスワードを入力してください")
            return redirect(url_for("signup"))

        # すでに存在するかチェック
        existing_user = db.session.query(User).filter_by(userName=userName).first()
        if existing_user:
            flash("このユーザー名はすでに使われています")
            return redirect(url_for("signup"))
    
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
        
        # ユーザーが存在しない場合
        if user is None:
            flash("ユーザー名が間違っています")
            return redirect(url_for("login"))
        
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