/* 全局共享可变状态：各视图模块统一经 S 读写（拆分前为 app.js 顶层变量） */
import { fmt } from "./api.js?v=202611012";

export const S = {
  todayView: new Date(),          // 今日视图当前日期
  weekAnchor: new Date(),         // 周视图锚点（取其所在周的周一）
  reqSeq: 0,                      // 请求序号，防止快速切换时旧响应覆盖新响应
  addType: "event",
  editing: null,                  // 编辑中的条目 {type, item}
  lessonSecTimes: {},             // 节次 -> [start, end]
  wkCtx: { minM: 480, maxM: 1050, PX_H: 56, dayItems: [], sections: [], gridTodos: [], dueTodos: [] },
  lessonCtx: null,                // {date, weekday, section, title, cancelled, cancel_id}
};
