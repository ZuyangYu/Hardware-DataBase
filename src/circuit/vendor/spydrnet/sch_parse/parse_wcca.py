"""
Version      Date          Author          Descriptions
-------      ----          ------          ------------
V00.00.0     2025/06/03    zaipeng.qu      适配WCCA自动化的模块整理
V00.00.1     2025/06/17    zaipeng.qu      模块中添加网络名称信息
V00.00.2     2025/07/14    zaipeng.qu      添加容差信息
V00.00.3     2025/09/22    yuxiang.li      添加电解电容逻辑
V00.00.4     2025/09/24    zaipeng.qu      返回的第二个变量只保留电容,_C是所有器件内容
V00.00.5     2025/12/25    yuxiang.li      添加手册名称获取
V00.00.6     2026/02/03    yuxiang.li      优化查找手册路径方法
"""
import spydrnet as sdn
import re
from collections import OrderedDict
import pandas as pd
import pymssql
import os


def ModelDataGet():
    """
    :return: database
    """
    server = "10.0.16.13"
    user = "hirainlib_ro"
    password = "hirainlib_ro"
    database = "hirainlib"
    conn = pymssql.connect(server, user, password, database, charset='GBK')
    cursor = conn.cursor()
    if cursor:
        print("数据库连接成功！")
    query = "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_TYPE='BASE TABLE'"
    tables = pd.read_sql(query, conn)['TABLE_NAME'].tolist()
    model_part = ['analog', 'capacitor', 'diode', 'Drive', 'Electromechanical', 'HWdesign', 'Inductor', 'Interface',
                  'Logic', 'Memory', 'Module', 'Oscillator', 'PCB',
                  'Power', 'PowerMosfet', 'Processor', 'Resistor', 'RF', 'transistor']
    # model_part = ['HirainModule']

    new_tables = []
    for item in model_part:
        if item in tables:
            new_tables.append(item)
    # 合并所有表
    data = pd.DataFrame()
    for table in new_tables:
        query = f'SELECT * FROM {table}'
        df = pd.read_sql(query, conn)
        df['table'] = table
        data = pd.concat([data, df], ignore_index=True)
    
    # print(data.columns)
    data = data.fillna('')
    # other_data = list(zip(data['ERP_NUM'], data['Datasheet']))
    model_dict = dict(zip(data['ERP_NUM'], data['Datasheet']))
    type_dict = dict(zip(data['ERP_NUM'], data['Type1']))
    # print(model_dict)
    return model_dict, type_dict


def convert_tuple(tuple_list, value_format='tuple', on_duplicate='overwrite'):
    """
    将元组列表转换为字典 (高级版本)

    参数:
    tuple_list (list): 包含三个字符串元素的元组列表
    value_format (str): 值格式，可选 'tuple', 'list', 'dict', 'string'
    on_duplicate (str): 处理重复键的策略，可选 'overwrite', 'ignore', 'combine'

    返回:
    dict: 转换后的字典
    """
    # 验证输入
    if not isinstance(tuple_list, list):
        raise TypeError("输入必须是列表")

    result_dict = {}

    for item in tuple_list:
        # 检查元素是否为元组
        if not isinstance(item, tuple):
            raise TypeError(f"列表元素必须是元组，但找到: {type(item)}")

        # 检查元组长度
        if len(item) != 3:
            raise ValueError(f"元组必须包含三个元素，但找到长度: {len(item)}")

        # 提取键和值
        key = item[0]
        value1 = item[1:]

        # 根据格式创建值
        if value_format == 'tuple':
            value = value1
        elif value_format == 'list':
            value = [value1]
        elif value_format == 'dict':
            value = {'value1': value1}
        elif value_format == 'string':
            value = f"{value1}"
        else:
            raise ValueError(f"不支持的 value_format: {value_format}")

        # 处理重复键
        if key in result_dict:
            if on_duplicate == 'overwrite':
                result_dict[key] = value
            elif on_duplicate == 'ignore':
                pass  # 不做任何操作，保留原始值
            elif on_duplicate == 'combine':
                # 组合值 (根据值格式处理)
                if value_format == 'tuple':
                    # 组合为元组列表
                    if isinstance(result_dict[key], tuple):
                        result_dict[key] = [result_dict[key]]
                    result_dict[key].append(value)
                elif value_format == 'list':
                    # 组合为列表的列表
                    result_dict[key].append(value)
                elif value_format == 'dict':
                    # 组合为字典列表
                    if isinstance(result_dict[key], dict):
                        result_dict[key] = [result_dict[key]]
                    result_dict[key].append(value)
                elif value_format == 'string':
                    # 组合为逗号分隔的字符串
                    result_dict[key] += f",{value}"
                else:
                    raise ValueError(f"不支持的 on_duplicate 选项: {on_duplicate}")
            else:
                result_dict[key] = value

    return result_dict


def deduplicate_lists(list_a, list_b):
    """进行保留标志的元组去重"""
    count_a = len(list_a)  # 原理图模块器件数量
    count_b = len(list_b)  # 库模块器件数量

    # 提取两个列表的第二个元组集合
    set_a = {elem[1] for elem in list_a}
    set_b = {elem[1] for elem in list_b}

    # 过滤列表a: 保留第二个元组不在set_b中的元素
    filtered_a = [elem for elem in list_a if elem[1] not in set_b]
    # 过滤列表b: 保留第二个元组不在set_a中的元素
    filtered_b = [elem for elem in list_b if elem[1] not in set_a]

    common_count = len(set_a & set_b)  # 相同器件数量

    return count_a, count_b, common_count, filtered_a, filtered_b


def decode_edf_content(edf_text):
    """进行中文乱码处理"""

    # 定义替换函数处理每个匹配项
    def decode_match(match):
        codes_str = match.group(1)  # 提取数字部分，如 "180%%229%%20%%190"
        codes = list(map(int, codes_str.split('%%')))  # 分割并转换为整数列表
        byte_data = bytes(codes)  # 转换为字节序列
        try:
            return byte_data.decode('gbk')  # 尝试GBK解码
        except UnicodeDecodeError:
            # 若解码失败，替换无法识别的字符
            return byte_data.decode('gbk', errors='replace')

    # 使用正则匹配所有%数字%序列，并替换为解码后的文本
    decoded_text = re.sub(
        r'%((?:\d+%%)+\d+)%',  # 匹配如%180%%229%的结构
        decode_match,
        edf_text
    )
    return decoded_text


def filter_U_dir(nested_list):
    """过滤不同芯片器件直接相连的网络"""
    # 构建特征字典：{特征元组: U开头首元素的集合}
    feature_dict = {}
    for sub in nested_list:
        if len(sub) >= 5:
            first = sub[0]
            if isinstance(first, str) and first.startswith('U'):
                feature = tuple(sub[2:5])  # 提取第2-5个元素
                if feature not in feature_dict:
                    feature_dict[feature] = set()  # 做去重
                feature_dict[feature].add(first)

    # 过滤符合条件的子列表
    result = []
    for sub in nested_list:
        if len(sub) < 5:
            result.append(sub)
            continue

        first = sub[0]
        feature = tuple(sub[2:5])

        # 判断删除条件
        if isinstance(first, str) and first.startswith('U'):
            if feature in feature_dict and len(feature_dict[feature]) > 1:
                continue  # 跳过需要删除的子列表

        result.append(sub)

    return result


def move_u(lst):
    """
    把U器件挪到前面
    """
    return sorted(lst,
                  key=lambda x: not (isinstance(x, str) and x.startswith('U')))


def euclidean_distance(point1, point2):
    """计算两个点的最小欧几里得距离"""
    return sum((a - b) ** 2 for a, b in zip(point1, point2)) ** 0.5


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
        # 合并所有匹配的组和当前集合 (需要逆序删除以避免索引错位)
        merged_set = current_set
        for index in reversed(matches):
            merged_set = merged_set.union(groups[index])
            del groups[index]
        groups.append(merged_set)
    # 将集合转换回列表，并保持元素顺序 (可选排序)
    result = [list(group) for group in groups]
    return result


def group_sub(nested_list):
    """
    进行相同连接器件整合
    :param nested_list:
    :return:
    """
    group_dict = {}
    for sublist in nested_list:
        # 将列表元素转换为元组 (如果是列表的话)
        key = (
            tuple(sublist[2]) if isinstance(sublist[2], list) else sublist[2],
            tuple(sublist[3]) if isinstance(sublist[3], list) else sublist[3],
            tuple(sublist[4]) if isinstance(sublist[4], list) else sublist[4],
            tuple(sublist[5]) if isinstance(sublist[5], list) else sublist[5],
        )

        # 分组存储
        if key not in group_dict:
            group_dict[key] = []
        group_dict[key].append(sublist)

    return list(group_dict.values())


def net_N(s):
    """
    检查字符串是否以'N'开头，其余部分均为阿拉伯数字

    参数:
        s (str): 待检查的字符串

    返回:
        bool: 如果符合条件返回True，否则返回False
    """
    # 检查字符串非空且以'N'开头
    if not s or s[0] != 'N':
        return False
    # 检查剩余部分是否全部是数字
    rest_part = s[1:]
    return rest_part.isdigit()  # 使用字符串的isdigit()方法检查是否为数字


def Get_data(edf_path):
    """
    获取原理图中的模块数据
    """
    NetList = sdn.parse(edf_path)

    data_dict = ModelDataGet()
    path_dict = data_dict[0]
    type_dict = data_dict[1]

    # 所需数据导出
    # 定义一些列表存储所需数据
    PartRef = []
    PartMPN = []
    PartFootprint = []
    PartValue = []
    PartERP = []
    PartClass = []
    PartType = []
    PartType_big = []
    PartTol = []
    PartDescription = []

    PowerName = []
    pin_ind = []
    pin_net = []
    wire_data = []

    PageNumber = []
    PageName = []
    PageTab = []

    # 电容信息
    PartRef_C = []
    PartType_C = []
    PartType_C_big = []
    PartMPN_C = []
    PartFootprint_C = []
    PartValue_C = []
    PartERP_C = []
    PartClass_C = []
    PartTol_C = []
    PartDescription_C = []
    DataSheet_C = []

    # 通过遍历网表结构获取所需信息
    tab = 0
    for library in NetList.libraries:
        for definition in library.get_definitions():
            for instance in definition.children:

                # 处理页码
                page_name = f'page_name{tab}'
                page_number = f'page_number{tab}'
                if instance.name == 'HirainTitleBlock':
                    keyword = 'Page Name'
                    nam = [entry for entry in instance['EDIF.properties'] if
                           entry.get("original_identifier") == keyword]
                    if nam:
                        p_name = nam[0]['value']
                    else:
                        p_name = 'None'
                    if p_name != 'None':
                        ind = p_name.find('_')
                        p_number = p_name[0:ind]
                    else:
                        p_number = 'None'
                    PageTab.append([page_name, p_name, page_number, p_number])
                    tab = tab + 1

                # 电源数据
                if instance.reference.name.startswith('VCC') or instance.reference.name.startswith('GND'):
                    VCC_name = instance['.NAME']
                    PowerName.append(VCC_name)

                # 器件的值获取
                if 'EDIF.properties.stringDisplay' in instance:
                    part_value = decode_edf_content(instance['EDIF.properties.stringDisplay'])
                    part_value = part_value.replace('37%', '')
                else:
                    part_value = 'None'

                # 器件的料号、封装以及等级获取
                if 'EDIF.properties' in instance:
                    keyword1 = 'PCB Footprint'
                    keyword2 = 'ERP NUM'
                    keyword3 = 'Class'
                    keyword4 = 'Manufacturer Part Number'
                    keyword5 = 'Part Type'
                    keyword6 = 'Tolerance'
                    keyword7 = 'Description'
                    footprint_str = [entry for entry in instance['EDIF.properties']
                                     if entry.get("original_identifier") == keyword1]
                    erp_str = [entry for entry in instance['EDIF.properties']
                               if entry.get("original_identifier") == keyword2]
                    class_str = [entry for entry in instance['EDIF.properties']
                                 if entry.get("original_identifier") == keyword3]
                    mpn_str = [entry for entry in instance['EDIF.properties']
                               if entry.get("original_identifier") == keyword4]
                    part_type_str = [entry for entry in instance['EDIF.properties']
                                     if entry.get("original_identifier") == keyword5]
                    tol_str = [entry for entry in instance['EDIF.properties']
                               if entry.get("original_identifier") == keyword6]
                    des_str = [entry for entry in instance['EDIF.properties']
                               if entry.get("original_identifier") == keyword7]
                    if footprint_str:
                        footprint = footprint_str[0]['value']
                    else:
                        footprint = 'None'

                    if erp_str:
                        erp = erp_str[0]['value']
                        part_path = path_dict.get(erp, '')
                        if part_path:
                            part_path_name = os.path.basename(part_path)
                        else:
                            part_path_name = 'None'
                    else:
                        erp = 'None'
                        part_path_name = 'None'

                    if class_str:
                        class_level = class_str[0]['value']
                    else:
                        class_level = 'None'

                    if mpn_str:
                        mpn = mpn_str[0]['value']
                    else:
                        mpn = 'None'

                    if part_type_str:
                        part_type = part_type_str[0]['value']
                    else:
                        part_type = 'None'

                    if tol_str:
                        tol = tol_str[0]['value']
                        tol_ind = tol.find('%')
                        tol = tol[0:tol_ind+1]
                    else:
                        tol = 'None'

                    if des_str:
                        des_all = des_str[0]['value']
                        if des_all:
                            match = re.search(r'\d', des_all)
                            if match:
                                start_ind = match.start()
                                end_ind = des_all.find('%')
                                des = des_all[start_ind:end_ind+1]
                            else:
                                des_all = 'None'
                                des = 'None'
                        else:
                            des_all = 'None'
                            des = 'None'
                    else:
                        des = 'None'

                else:
                    footprint = 'None'
                    erp = 'None'
                    part_path_name = 'None'
                    class_level = 'None'
                    mpn = 'None'
                    part_type = 'None'
                    tol = 'None'
                    des = 'None'
                    des_all = 'None'

                # 位号
                if 'EDIF.designator' in instance:
                    part_ref = instance['EDIF.designator']
                    for pin in instance.pins:
                        # pin的坐标数据获取
                        for port in pin.get_ports():
                            if 'EDIF.designator' in port.data:
                                ind = port.data['EDIF.designator']  # pin序号
                                pin_ind.append(ind)

                                # 定位到正确的pin序号
                                if 'EDIF.portnums' in instance:
                                    pin_count = instance['EDIF.portnums']  # pin数量
                                    for pin_turn in range(1, pin_count + 1):
                                        if (instance[f'EDIF.port{pin_turn}.portInstance.designator.stringDisplay']
                                                == ind):
                                            pin_axis = instance[
                                                (
                                                    f'EDIF.port{pin_turn}.portInstance.designator.stringDisplay.display'
                                                    f'.origin.pt')
                                            ]

                                        # 遍历获取网络与cable端点数据
                                        if pin.wire is not None:
                                            wire_node = []
                                            wire_name = pin.wire.cable.name
                                            figure_count = pin.wire.cable['EDIF.figurenums']
                                            for figure_tab in range(1, figure_count + 1):
                                                figure_node = []
                                                node_count = pin.wire.cable[
                                                    (f'EDIF.figure{figure_tab}.figure.path'
                                                     f'.pointlist.pointnums')]
                                                for node_tab in range(1, node_count + 1):
                                                    figure_node.append(pin.wire.cable[
                                                                           f'EDIF.figure{figure_tab}.figure.path'
                                                                           f'.pointlist.point{node_tab}.pt'])
                                                wire_node.append(figure_node)
                                            # 对连接在一起的网络cable进行合并
                                            wire_node = merge_sub(wire_node)

                                            # 获取正确的cable组合
                                            correct_wire = 'None'
                                            if len(wire_node) == 1:
                                                correct_wire = wire_node[0]
                                            else:
                                                for wire in wire_node:
                                                    for node in wire:
                                                        if euclidean_distance(pin_axis, node) <= 25:
                                                            correct_wire = wire
                                            per_wire_data = tuple(correct_wire)
                                            # if part_ref in ['R3410', 'U3401']:
                                            #     print('part_ref:', part_ref)
                                            #     print('wire_name:', wire_name)
                                            #     print('ind:', ind)
                                            #     print('pin_axis: ', pin_axis)
                                            #     print('wire_node:', wire_node)
                                            #     print('wire_name:', wire_name)
                                            #     print('correct_wire:', correct_wire)
                                            #     print('per_wire_data:', per_wire_data)
                                            #     print('-----------------------------------------')

                                # if part_ref == "U301":
                                #     print(part_ref)
                                #     print(part_path_name)
                                PartRef.append(part_ref)
                                PartMPN.append(mpn)
                                PartFootprint.append(footprint)
                                PartValue.append(decode_edf_content(part_value))
                                PartERP.append(erp)
                                PartClass.append(class_level)
                                PartType.append(decode_edf_content(part_type))
                                if erp == 'None':
                                    PartType_big.append('None')
                                else:
                                    PartType_big.append(decode_edf_content(type_dict[erp]))
                                PartTol.append(tol)
                                PartDescription.append(des)

                                pin_net.append(wire_name)
                                PageNumber.append(page_number)
                                PageName.append(page_name)
                                wire_data.append(per_wire_data)

                                PartRef_C.append(part_ref)
                                PartType_C.append(decode_edf_content(part_type))
                                PartMPN_C.append(mpn)
                                PartFootprint_C.append(footprint)
                                PartValue_C.append(decode_edf_content(part_value))
                                PartERP_C.append(erp)
                                if erp == 'None':
                                    PartType_C_big.append('None')
                                else:
                                    PartType_C_big.append(decode_edf_content(type_dict[erp]))
                                PartClass_C.append(class_level)
                                PartTol_C.append(tol)
                                PartDescription_C.append(des)
                                if part_path_name == 'None':
                                    nums = re.findall(r'\d{9}', des_all)
                                    for num in nums:
                                        part_path = path_dict.get(num, '')
                                        if part_path:
                                            part_path_name = os.path.basename(part_path)
                                            break
                                DataSheet_C.append(part_path_name)

                elif 'EDIF.designator.stringDisplay' in instance:
                    part_ref = instance['EDIF.designator.stringDisplay']
                    for pin in instance.pins:
                        # pin的坐标数据获取
                        for port in pin.get_ports():
                            if 'EDIF.designator' in port.data:
                                ind = port.data['EDIF.designator']  # pin序号
                                pin_ind.append(ind)

                                # 定位到正确的pin序号
                                if 'EDIF.portnums' in instance:
                                    pin_count = instance['EDIF.portnums']  # pin数量
                                    for pin_turn in range(1, pin_count + 1):
                                        if (instance[f'EDIF.port{pin_turn}.portInstance.designator.stringDisplay']
                                                == ind):
                                            pin_axis = instance[
                                                (
                                                    f'EDIF.port{pin_turn}.portInstance.designator.stringDisplay.display'
                                                    f'.origin.pt')
                                            ]

                                        # 遍历获取网络与cable端点数据
                                        if pin.wire is not None:
                                            wire_node = []
                                            wire_name = pin.wire.cable.name
                                            figure_count = pin.wire.cable['EDIF.figurenums']
                                            for figure_tab in range(1, figure_count + 1):
                                                figure_node = []
                                                node_count = pin.wire.cable[
                                                    (f'EDIF.figure{figure_tab}.figure.path'
                                                     f'.pointlist.pointnums')]
                                                for node_tab in range(1, node_count + 1):
                                                    figure_node.append(pin.wire.cable[
                                                                           f'EDIF.figure{figure_tab}.figure.path'
                                                                           f'.pointlist.point{node_tab}.pt'])
                                                wire_node.append(figure_node)
                                            # 对连接在一起的网络cable进行合并
                                            wire_node = merge_sub(wire_node)

                                            # 获取正确的cable组合
                                            correct_wire = 'None'
                                            if len(wire_node) == 1:
                                                correct_wire = wire_node[0]
                                            else:
                                                for wire in wire_node:
                                                    for node in wire:
                                                        if euclidean_distance(pin_axis, node) <= 25:
                                                            correct_wire = wire
                                            per_wire_data = tuple(correct_wire)
                                            # if part_ref in ['R3410', 'U3401']:
                                            #     print('part_ref:', part_ref)
                                            #     print('wire_name:', wire_name)
                                            #     print('ind:', ind)
                                            #     print('pin_axis: ', pin_axis)
                                            #     print('wire_node:', wire_node)
                                            #     print('wire_name:', wire_name)
                                            #     print('correct_wire:', correct_wire)
                                            #     print('per_wire_data:', per_wire_data)
                                            #     print('-----------------------------------------')

                                # if part_ref == "U301":
                                #     print(part_ref)
                                #     print(part_path_name)
                                PartRef.append(part_ref)
                                PartMPN.append(mpn)
                                PartFootprint.append(footprint)
                                PartValue.append(decode_edf_content(part_value))
                                PartERP.append(erp)
                                PartClass.append(class_level)
                                PartType.append(decode_edf_content(part_type))
                                if erp == 'None':
                                    PartType_big.append('None')
                                else:
                                    PartType_big.append(decode_edf_content(type_dict[erp]))
                                PartTol.append(tol)
                                PartDescription.append(des)

                                pin_net.append(wire_name)
                                PageNumber.append(page_number)
                                PageName.append(page_name)
                                wire_data.append(per_wire_data)

                                PartRef_C.append(part_ref)
                                PartType_C.append(decode_edf_content(part_type))
                                PartMPN_C.append(mpn)
                                PartFootprint_C.append(footprint)
                                PartValue_C.append(decode_edf_content(part_value))
                                PartERP_C.append(erp)
                                if erp == 'None':
                                    PartType_C_big.append('None')
                                else:
                                    PartType_C_big.append(decode_edf_content(type_dict[erp]))
                                PartClass_C.append(class_level)
                                PartTol_C.append(tol)
                                PartDescription_C.append(des)
                                if part_path_name == 'None':
                                    nums = re.findall(r'\d{9}', des_all)
                                    for num in nums:
                                        part_path = path_dict.get(num, '')
                                        if part_path:
                                            part_path_name = os.path.basename(part_path)
                                            break
                                DataSheet_C.append(part_path_name)

    # 所有器件的信息
    df = list(zip(PartRef, pin_ind, pin_net, wire_data, PageName, PageNumber))
    df_all = list(zip(PartRef, PartMPN, PartFootprint, PartValue, PartERP, PartType, PartType_big, PartTol, pin_ind,
                      pin_net, PageName, PageNumber))
    df_all_C = list(zip(PartRef_C, PartType_C, PartType_C_big, DataSheet_C, PartFootprint_C, PartValue_C, PartERP_C,
                        PartClass_C, PartTol_C, PartDescription_C))

    # 电容模块信息
    df_dict = convert_tuple(list(OrderedDict.fromkeys(list(zip(PartRef_C, PartType_C, PartDescription_C)))))

    # 页码与页名更新
    for ia in range(0, len(df)):
        df[ia] = list(df[ia])
        df_all[ia] = list(df_all[ia])
        for tabs in PageTab:
            if df[ia][-1] == tabs[2] and df[ia][-2] == tabs[0]:
                df[ia][-1] = tabs[3]
                df[ia][-2] = tabs[1]
            if df_all[ia][-1] == tabs[2] and df_all[ia][-2] == tabs[0]:
                df_all[ia][-1] = tabs[3]
                df_all[ia][-2] = tabs[1]
        df[ia] = tuple(df[ia])
        df_all[ia] = tuple(df_all[ia])

    # 过滤直连的芯片器件
    u_filtered = filter_U_dir(df)

    # 相同连接关系的器件整合
    process = group_sub(u_filtered)
    ref = []
    for a in process:
        ref_line = []
        for a1 in a:
            tar = a1[0]
            if 'U' in tar:
                if '-' in tar:
                    ind = tar.find("-")
                    tar = tar[0:ind]
                else:
                    for i in range(1, len(tar)):
                        if tar[i].isdigit():
                            pass
                        else:
                            tar = tar[0:i]

            ref_line.append(tar)
        ref.append(ref_line)

    # 模块整合
    out_list = merge_sub(ref)
    out_list = list(out_list)
    out_plus_net = []
    for part_list in out_list:
        part_out = part_list.copy()
        part_out = move_u(part_out)
        for i, part in enumerate(part_list):
            for line in df:
                if part == line[0]:
                    # 电容信息添加
                    # if part.startswith('C'):
                    #     part_out[i] = ' '.join([part_list[i], df_dict[part_list[i]][0], df_dict[part_list[i]][1]])
                    # 非默认网络命名
                    if not net_N(line[2]):
                        part_out.append(line[2])
        out_plus_net.append(part_out)

    return out_plus_net, df_all_C


# edf_path_ = r"E:\zaipeng.qu\sch_checker_trigger\test_data\20260318\825505035_ISSC_SCH_260318.EDF"
# Get_data(edf_path_)