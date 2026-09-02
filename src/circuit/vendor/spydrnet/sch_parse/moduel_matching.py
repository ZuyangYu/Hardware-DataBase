"""
Version      Date          Author          Descriptions
-------   ----------     ----------      ------------------
V00.00.1  2025/06/24     zaipeng.qu      复用moduel_matchingtest.py V006版本，做适配修改
"""
from difflib import SequenceMatcher
import random
import string
import re

def merge_sub(nested_list):
    """合并具有公共元素的子列表"""
    # 将每个子列表转换为集合，并逐步合并有交集的集合
    groups = []
    for sublist in nested_list:
        current_set = set(sublist)
        # 查找所有与当前集合有交集的组的索引
        matches = []
        for i, group_set in enumerate(groups):
            if current_set & group_set:  # 检查是否有交集
                matches.append(i)
        # 合并所有匹配的组和当前集合（需要逆序删除以避免索引错位）
        merged_set = current_set
        for index in reversed(matches):
            merged_set = merged_set.union(groups[index])
            del groups[index]
        groups.append(merged_set)
    # 将集合转换回列表，并保持元素顺序（可选排序）
    result = [list(group) for group in groups]
    return result

def match_lst(list1, list2):
    """
    匹配两个嵌套列表中具有相同前三个元素的子列表

    参数:
    list1: 第一个嵌套列表，每个子列表包含3个元素
    list2: 第二个嵌套列表，每个子列表包含4个元素

    返回:
    第二个列表中所有前三个元素与第一个列表中任何子列表匹配的子列表
    """
    # 将第一个列表的子列表转换为元组并创建查找集
    tuple_set = set(tuple(sublist) for sublist in list1)

    # 使用列表推导式匹配第二个列表
    matched = [sublist for sublist in list2 if tuple(sublist[:3]) in tuple_set]
    return matched

def group_sub(nested_list, k):
    """
    进行相同连接器件整合，将特定位置元素相同的子列表合并，标志位可选
    :param nested_list:待合并的元组列表
    :k:标志位
    :return:合并后的元组列表
    """
    group_dict = {}
    for sublist in nested_list:
        # 将列表元素转换为元组（如果是列表的话）
        key = (
            tuple(sublist[k]) if isinstance(sublist[k], list) else sublist[k],
        )

        # 分组存储
        if key not in group_dict:
            group_dict[key] = []
        group_dict[key].append(sublist)

    return list(group_dict.values())

def move_u(lst):
    """
    把U器件挪到前面
    """
    return sorted(lst,
                  key=lambda x: not (isinstance(x, str) and x.startswith('U')))

def filter_digits(text):
    """遍历字符串，过滤掉所有数字字符"""
    return ''.join(char for char in text if not char.isdigit())

def net_N(s):
    """
    检查字符串是否以'N'开头，其余部分均为阿拉伯数字
    参数:
        s (str): 待检查的字符串
    返回:
        bool: 如果符合条件返回True，否则返回False
    """
    # 检查字符串非空且以'N'开头
    if s[0] == 'N' and len(s) >= 5 and s[1:].isdigit():
        return False
    else:
        return True

def generate_random_code():
    """
    生成9位随机编码（字母和数字组合）

    返回:
        str: 9位随机编码
    """
    # 定义所有可能的字符：大小写字母和数字
    characters = string.ascii_letters + string.digits

    # 从字符集中随机选择9个字符组合成编码
    random_code = ''.join(random.choices(characters, k=9))

    return random_code

def extract_digits(input_string):
    """
    提取字符串中的所有阿拉伯数字（0-9）

    参数:
        input_string (str): 包含数字和非数字字符的字符串

    返回:
        str: 仅包含阿拉伯数字的新字符串
    """
    # 使用列表推导式筛选出数字字符
    digits_only = [char for char in input_string if char.isdigit()]

    # 将数字字符列表连接成字符串
    return ''.join(digits_only)

def cell_code(cell_list, pair_code, pair_net):
    """
    进行器件连接信息硬编码操作
    :param cell_list:器件参数嵌套列表，每个子列表为器件参数元组：(位号, pin_number, pin_net)
    :param pair_code:相似名称编码字典,{网络名:编码}
    :param pair_net:已编码网络列表
    :return:编码后的器件列表
    """
    # 整合独立器件的每个pin数据
    combine_list = group_sub(cell_list, 0)
    # print('combine_list:', combine_list)

    # 提取器件及器件对应的连接网络
    combine_process = []
    combine_process_dir = []
    for part in combine_list:
        # pin数量大于2的器件
        if len(part) > 2:
            for pin in part:
                if 'GND' not in pin[-1]:
                    for cell in pin[1:-1]:
                        combine_process.append((f'{pin[0]}-{cell}', pin[-1]))
        # pin数量小于2的器件
        else:
            for pin in part:
                if 'GND' not in pin[-1]:
                    combine_process.append((pin[0], pin[-1]))
    # print('combine_process:', combine_process)
    # 存储网络信息
    combine_net = combine_process.copy()

    # 相同连接的网络整合为子列表
    combine_sch = group_sub(combine_process, 1)
    # print('combine_sch:', combine_sch)
    # 将相同连接的器件位号整合为同一个子列表，不保留网络
    combine_cell = []
    for part in combine_sch:
        middle_list = []
        for cell in part:
            middle_list.append(cell[0])
        middle_list = move_u(middle_list)
        combine_cell.append(middle_list)
    # print('combine_cell: ', combine_cell)

    # 消除电源网络的干扰
    for part in combine_process:
        part_pro = list(part)
        if net_N(part_pro[1]): # 判断是否为自定义网络
            part_pro[1] = generate_random_code() # 字符串替换随机编码
            combine_process_dir.append(part_pro)
        else:
            combine_process_dir.append(part_pro)
    # print('combine_process_dir:', combine_process_dir)
    combine_sch_dir = group_sub(combine_process_dir, 1)
    # print('combine_sch_dir:', combine_sch_dir)
    combine_cell_dir = []
    for part in combine_sch_dir:
        middle_list = []
        for cell in part:
            middle_list.append(cell[0])
        middle_list = move_u(middle_list)
        combine_cell_dir.append(middle_list)

    # 相同连接的模块进行合并
    combine_cell_dir = merge_sub(combine_cell_dir)
    # print('combine_cell_dir: ', combine_cell_dir)

    # 进行器件直连连接标志硬编码
    dir_code_list = []
    for part in combine_cell_dir:
        # 芯片开头的模块情况
        process_list = part.copy()
        for ref in part:
            if ref.startswith('U'):
                # 清洗芯片器件，芯片核心器件不进行位置编码
                process_list.remove(ref)
        for cell in process_list:
            part_process = part.copy()
            part_process.remove(cell)
            code_str = ''
            code_str_list = [cell]
            pin_tab = []
            for cel in part_process:
                if "-" in cel:
                    ind = cel.find("-")
                    # 清洗数字进行编码
                    code_s = filter_digits(cel[:ind]) + cel[ind:]
                    # 添加芯片器件的pin通道标志
                    if 'U' in code_s:
                        pin_tab.append(code_s)
                else:
                    # 清洗数字进行编码
                    code_s = filter_digits(cel)
                code_str = code_str + code_s
            if pin_tab:
                # 添加芯片通道标志
                code_str_list.append(pin_tab)
            else:
                code_str_list.append(['no_cly'])
            code_str_list.append(code_str)
            dir_code_list.append(code_str_list)
    # 编码后位号清洗
    for i in range(0, len(dir_code_list)):
        if '-' in dir_code_list[i][0]:
            ind1 = dir_code_list[i][0].find('-')
            dir_code_list[i][0] = dir_code_list[i][0][:ind1]
    # 器件编码合并
    # print('dir_code_list;', dir_code_list)
    dir_code_list = group_sub(dir_code_list, 0)
    # print('dir_code_list;', dir_code_list)

    # 将同一pin通道的器件进行组合,并将U器件提前
    # combine_cell_merge = merge_sub(combine_cell)
    combine_cell_merge = combine_cell # pin通道编码不做merge操作
    for i_c in range(0, len(combine_cell_merge)):
        combine_cell_merge[i_c] = move_u(combine_cell_merge[i_c])
    # print('combine_cell_merge: ', combine_cell_merge)
    # 进行器件PIN通道连接标志硬编码
    cell_code_list = []
    for part in combine_cell_merge:
        # 芯片开头的模块情况
        process_list = part.copy()
        for ref in part:
            if ref.startswith('U'):
                # 清洗芯片器件，芯片核心器件不进行位置编码
                process_list.remove(ref)
        for cell in process_list:
            part_process = part.copy()
            part_process.remove(cell)
            code_str = ''
            code_str_list = [cell]
            pin_tab = []
            for cel in part_process:
                if "-" in cel:
                    ind = cel.find("-")
                    # 清洗数字进行编码
                    code_s = filter_digits(cel[:ind]) + cel[ind:]
                    # 添加芯片器件的pin通道标志
                    if 'U' in code_s:
                        pin_tab.append(code_s)
                else:
                    # 清洗数字进行编码
                    code_s = filter_digits(cel)
                code_str = code_str + code_s
            if pin_tab:
                # 添加芯片通道标志
                code_str_list.append(pin_tab)
            else:
                code_str_list.append(['no_cly'])
            code_str_list.append(code_str)
            # print('code_str_list:', code_str_list)
            cell_code_list.append(code_str_list)

    # 编码后位号清洗
    for i in range(0, len(cell_code_list)):
        if '-' in cell_code_list[i][0] and 'J' not in cell_code_list[i][0]:
            ind1 = cell_code_list[i][0].find('-')
            cell_code_list[i][0] = cell_code_list[i][0][:ind1]

    # 同一器件编码整合
    code_list = []
    # print('cell_code_list;', cell_code_list)
    cell_code_list = group_sub(cell_code_list, 0)
    # print('cell_code_list;', cell_code_list)

    # 输出整合
    for part in cell_code_list:
        # 添加网络标志
        net_tab = []
        for part_net in combine_net:
            if part[0][0] == part_net[0] and part_net[1] in pair_net:
                net_tab.append(pair_code[part_net[1]])
        # print('part:', part)
        middle_str = part[0][2] # pin通道位置编码
        dir_str = '' # 直连位置编码
        middle_pin = part[0][1] # pin通道
        for ce in part[1:]:
            middle_str = middle_str + '_' + ce[2]
            middle_pin = middle_pin + ce[1]
        for part_dir in dir_code_list:
            if part[0][0] == part_dir[0][0]:
                dir_str = part_dir[0][2] # 位置编码
                middle_pin = middle_pin + part_dir[0][1]
                for cd in part_dir[1:]:
                    middle_pin = middle_pin + cd[1]
                    dir_str = dir_str + '_' + cd[2]
        # pin通道去重
        middle_pin = list(dict.fromkeys(middle_pin))
        if 'no_cly' in middle_pin:
            middle_pin.remove('no_cly')
        # [位号, 端口编码列表, 网络连接标志列表, 类型, pin通道位置编码, 直连位置编码]
        middle_code_list = [part[0][0], middle_pin, net_tab, filter_digits(part[0][0]), middle_str, dir_str]
        # print('middle_code_list:', middle_code_list)
        code_list.append(middle_code_list)
    return code_list

def sim_calculate(list0, list1):
    """计算待匹配器件与候选器件之间的最佳匹配结果。

    :param list0:单个模块器件列表: [位号, 端口编码列表, 网络连接标志, 类型, pin通道位置编码, 直连位置编码, 封装, 器件值]
    :param list1:待布局模块器件嵌套列表, 子列表: [位号, 端口编码列表, 网络连接标志, 类型, pin通道位置编码, 直连位置编码, 封装, 器件值]
    :return:编码后的器件列表
    """

    # 类型、网络、封装和值相同的候选器件才进入位置编码比较。
    process_result = []
    for res in list1:
        if (
            lst_compare(res[1], list0[1])
            and lst_compare(res[2], list0[2])
            and clean_str(res[3]) == clean_str(list0[3])
            and clean_str(res[6]) == clean_str(list0[6])
        ):
            process_result.append(res)

    if not process_result:
        return "pass"

    # 先按 pin 通道编码相似度筛选，再按直连位置和位号消歧。
    sim_result_pin = []
    for res in process_result:
        scored = res.copy()
        scored.append(str_compare(list0[4], res[4]))
        sim_result_pin.append(scored)

    code_pin = [res[-1] for res in sim_result_pin]
    index_pin = max_pos(code_pin)
    if len(index_pin) == 1:
        return sim_result_pin[index_pin[0]][0:7]

    sim_result_dir = []
    for ind_d in index_pin:
        candidate = sim_result_pin[ind_d]
        scored = candidate.copy()
        sim_cd = str_compare(list0[5], candidate[5])
        count_ind = 1 + abs(len(list0[5]) - len(candidate[5]))
        scored.append(sim_cd / count_ind)
        sim_result_dir.append(scored)

    code_dir = [res[-1] for res in sim_result_dir]
    index_dir = max_pos(code_dir)
    if len(index_dir) == 1:
        return sim_result_dir[index_dir[0]][0:7]

    # 最后用位号数字部分的距离稳定消歧。
    sim_dir_list = [sim_result_dir[ind_cd] for ind_cd in index_dir]
    ref_distances = []
    for candidate in sim_dir_list:
        try:
            distance = abs(float(extract_digits(list0[0])) - float(extract_digits(candidate[0])))
        except (TypeError, ValueError):
            distance = float("inf")
        ref_distances.append(distance)
    return sim_dir_list[ref_distances.index(min(ref_distances))][0:7]


def max_pos(lst):
    """返回列表中所有最大值的位置序号"""
    max_val = max(lst)
    return [i for i, x in enumerate(lst) if x == max_val]

def str_compare(str1, str2):
    """字符串相似度计算"""
    # 首先比较长度是否相同
    if len(str1) == len(str2) and sorted(str1) == sorted(str2):
        sim_rio = 1
    else:
        sim_rio = SequenceMatcher(None, str1, str2).ratio()
    return sim_rio

def lst_compare(lst1, lst2, key1=None):
    """判断两个列表中的元素是否完全一致支持复杂元素的比较"""
    if len(lst1) != len(lst2):
        return False
    # 自定义排序键函数
    if key1 is None:
        key1 = lambda x: str(x) # 默认使用字符串表示进行排序
    return sorted(lst1, key=key1) == sorted(lst2, key=key1)

def clean_str(text):
    """匹配非字母数字、非-、非_的字符并替换为空字符串"""
    return re.sub(r'[^a-zA-Z0-9_-]', '', text)

def similarity_net_pair(list1, list2, min_similarity=0.3):
    """
    在两个字符串列表之间找到一一对应的最高相似度匹配对
    参数:
        list1 (list): 第一个字符串列表
        list2 (list): 第二个字符串列表
        min_similarity (float): 最低相似度阈值(0-1)

    返回:
        list: 嵌套列表，每个子列表包含最相似的字符串对
    """
    # 检查空列表
    if not list1 or not list2:
        return []

    # 创建剩余索引集合。
    remaining_i = set(range(len(list1)))
    remaining_j = set(range(len(list2)))
    matched_pairs = []

    while remaining_i and remaining_j:
        best_similarity = min_similarity
        best_pair = None
        best_i = None
        best_j = None

        for i in list(remaining_i):
            for j in list(remaining_j):
                similarity = SequenceMatcher(None, list1[i], list2[j]).ratio()
                if similarity > best_similarity:
                    best_similarity = similarity
                    best_pair = [list1[i], list2[j]]
                    best_i = i
                    best_j = j

        if best_pair is None:
            break

        matched_pairs.append(best_pair)
        remaining_i.remove(best_i)
        remaining_j.remove(best_j)

    return matched_pairs


def matching_con(dsn_list, model_list):
    """
    模块库与待布局器件匹配，匹配连接关系相同的对应器件
    :param dsn_list: dict, 原理图模块信息列表
    :param model_list: dict, 模块库模块信息列表
    :return matching_new_dict: dict, 待布局板的模块器件信息
    """
    model_net = []  # 定义一个用于存储模块自定义网络的列表
    sch_net = []  # 定义一个用于存储布局模块自定义网络的列表

    # 获取编码所需信息
    dsn_list_code = []
    part_ref_dsn = []
    value_dsn = []
    footprint_dsn = []
    other_data_dsn = []
    for part in dsn_list:
        dsn_list_code.append([part[0], part[5], part[6]])
        part_ref_dsn.append(part[0])
        value_dsn.append(part[3])
        footprint_dsn.append(part[2])
        other_data_dsn.append(part[1:5])
        if net_N(part[6]) and 'GND' not in part[6]:
            sch_net.append(part[6])
    dsn_footprint_dict = dict(zip(part_ref_dsn, footprint_dsn))
    dsn_value_dict = dict(zip(part_ref_dsn, value_dsn))
    dsn_all_dict = dict(zip(part_ref_dsn, other_data_dsn))
    sch_net = list(dict.fromkeys(sch_net))  # 去重

    model_list_code = []
    part_ref_model = []
    value_model = []
    footprint_model = []
    other_data_model = []
    for part in model_list:
        model_list_code.append([part[0], part[5], part[6]])
        part_ref_model.append(part[0])
        value_model.append(part[3])
        footprint_model.append(part[2])
        other_data_model.append(part[1:5])
        if net_N(part[6]) and 'GND' not in part[6]:
            model_net.append(part[6])
    model_footprint_dict = dict(zip(part_ref_model, footprint_model))
    model_value_dict = dict(zip(part_ref_model, value_model))
    model_all_dict = dict(zip(part_ref_model, other_data_model))
    model_net = list(dict.fromkeys(model_net))  # 去重

    # 获取核心器件
    core_component_dsn = ''
    core_component_model = ''
    for part in dsn_list:
        if part[0].startswith('U'):
            core_component_dsn = part[0]
            break
    for part in model_list:
        if part[0].startswith('U'):
            core_component_model = part[0]
            break

    # 匹配相似度最高的网络名
    net_sim = similarity_net_pair(sch_net, model_net)

    # 对相同网络名进行归一化编码
    net_list = []
    net_code = []
    for pair in net_sim:
        n_code = generate_random_code()
        for net in pair:
            net_list.append(net)
            net_code.append(n_code)
    # 已编码网络字典
    net_code_dict = dict(zip(net_list, net_code))
    # 已编码网络列表
    net_code_List = list(dict.fromkeys(net_list))

    # 模块器件位置关系编码
    model_code = cell_code(model_list_code, net_code_dict, net_code_List)
    sch_code = cell_code(dsn_list_code, net_code_dict, net_code_List)

    # 添加封装与值信息
    for i_m in range(0, len(model_code)):
        # print(model_code[i_m])
        # print(model_footprint_dict)
        # print(model_footprint_dict[model_code[i_m][0]])
        if model_code[i_m][0] in part_ref_model:
            model_code[i_m].append(model_footprint_dict[model_code[i_m][0]])
    for i_s in range(0, len(sch_code)):
        if sch_code[i_s][0] in part_ref_dsn:
            sch_code[i_s].append(dsn_footprint_dict[sch_code[i_s][0]])

    # 器件位置关系编码相似度计算(模块匹配)
    part_only_model_list = []  # 只有模块有的器件位号
    match_list = [[core_component_model, 'paired', core_component_dsn]]
    km = 0
    NO_match_count = 0
    for line in model_code:
        # print('次数：', km)
        if sch_code:
            match_res = sim_calculate(line, sch_code)
            km = km + 1
            if match_res == 'pass':
                NO_match_count = NO_match_count + 1
                part_only_model_list.append(line[0])
                continue
            else:
                match_list.append([line[0], 'paired', match_res[0]])
                sch_code.remove(match_res)
        else:
            break

    # 匹配阈值计算
    match_result = (len(model_code) - NO_match_count) / len(model_code)
    # print('匹配阈值：', match_result)

    if match_result <= 0.6:
        return 200
    else:
        # 取值对不上的器件
        value_different_str = ''
        value_different_list = []

        for part_pair in match_list:
            if model_value_dict[part_pair[0]] != dsn_value_dict[part_pair[2]]:
                value_different_list.append(f"{part_pair[0]}({model_value_dict[part_pair[0]]})-"
                                            f"{part_pair[2]}({dsn_value_dict[part_pair[2]]})")
        for i1 in range(0, len(value_different_list), 3):
            row = value_different_list[i1:i1 + 3]
            value_different_str = value_different_str + (', '.join(map(str, row))) + '\n'

        # 只有原理图有的器件位号
        part_only_dsn = ''
        part_only_dsn_list = []
        for part in sch_code:
            part_only_dsn_list.append(part[0])
        for i2 in range(0, len(part_only_dsn_list), 10):
            row = part_only_dsn_list[i2:i2 + 10]
            part_only_dsn = part_only_dsn + (', '.join(map(str, row))) + '\n'

        # 只有模块库有的器件位号
        part_only_model = ''
        for i3 in range(0, len(part_only_model_list), 10):
            row = part_only_model_list[i3:i3 + 10]
            part_only_model = part_only_model + (', '.join(map(str, row))) + '\n'

        # value_different_str = value_different_str + '*'
        # value_different_str = value_different_str.replace(', *', '')
        #
        # part_only_dsn = part_only_dsn + '*'
        # part_only_dsn = part_only_dsn.replace(', *', '')
        #
        # part_only_model = part_only_model + '*'
        # part_only_model = part_only_model.replace(', *', '')

        return value_different_str, part_only_dsn, part_only_model, match_result
