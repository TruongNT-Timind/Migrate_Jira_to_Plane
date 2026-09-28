# Tool web migrate Jira sang Plane

Số ticket trên Plane do Plane tự cấp, không trùng số Jira. Trong mô tả mỗi work item có `Jira key`, `Jira created`, `Jira updated` và `Jira reporter` để đối chiếu.

Ví dụ project đã migrate: Jira **ADM** (`https://ittimind.atlassian.net`) sang Plane **Facebook Ads Managements**, identifier **FACEBOOKAD**.

## Chạy tool

Đứng ở thư mục chứa `server.py`:

```bash
python3 server.py
```

Mở http://127.0.0.1:8765/

Trang có bốn phần: kết nối, kéo dữ liệu Jira, mapping status, ghi sang Plane. Bấm **Lưu cấu hình** thì giá trị trên màn hình được ghi vào `data/config.json` ở thư mục cha của thư mục chứa `server.py`. File này ghi đè `.env` cùng chỗ đó khi có giá trị.

Chạy lại migrate thì ticket đã có trong `data/checkpoint.json` được bỏ qua. Muốn ghi lại từ đầu một project thì xóa các dòng tương ứng trong file đó trước.

## Lấy thông tin config

Điền trên màn hình, rồi **Kiểm tra Jira** và **Kiểm tra Plane**. Cả hai dòng kết quả phải màu xanh trước khi kéo project.

### Jira

| Ô trên màn hình | Lấy ở đâu |
| --- | --- |
| Site URL | Phần đầu URL Jira Cloud, không có `/jira` hay dấu `/` cuối. Project ADM là `https://ittimind.atlassian.net`. |
| Email | Email đang đăng nhập Atlassian. Xem tại [Profile](https://id.atlassian.com/manage-profile/profile-and-visibility). |
| API token | [API tokens](https://id.atlassian.com/manage-profile/security/api-tokens) → **Create API token**. Chọn token không scope. Copy ngay lúc tạo. |
| Project key | Chữ in hoa trong URL, ví dụ `.../projects/ADM/boards/1` thì key là `ADM`. Hoặc **Project settings → Details**, ô **Key**. |

Email và token phải cùng một tài khoản. Token có scope không gọi được thẳng site `*.atlassian.net`.

### Plane

URL project có dạng `https://plane.timind.co/timind/projects/a5e14bdd-b090-46f6-aef4-313b7d745abf/issues/`.

| Ô trên màn hình | Lấy từ URL / trang cài đặt |
| --- | --- |
| API URL | `https://plane.timind.co` |
| Workspace slug | `timind` |
| Project id | `a5e14bdd-b090-46f6-aef4-313b7d745abf` |
| API key | [Personal access tokens](https://plane.timind.co/timind/settings/account/api-tokens/). Copy ngay lúc tạo. |

Tài khoản của API key phải là member của project đích. Project cần bật **Modules**.

## Các bước migrate

1. Đứng ở thư mục chứa `server.py`, chạy `python3 server.py`, mở http://127.0.0.1:8765/, điền config và lưu.
2. Bấm **Kéo project**.
3. Chọn loại issue sẽ ghi sang Plane. Bỏ chọn loại không cần. Trên ADM, loại tên **Issue** là bug (sub-task, mô tả loại là "Bug of US/task"). **Sub-task** là việc con. Sub-task của loại bị bỏ cũng không được ghi.
4. Kiểm tra mapping status theo bảng bên dưới. Tool điền sẵn theo template này.
5. Bấm **Xem trước**, đọc log, rồi **Migrate lên Plane**.

## Mapping đang dùng

Epic thành Module. Story, Task, Sub-task và Issue thành work item. Sub-task có `parent` là work item của issue cha. Component thành label `component:<tên>`. Label Jira giữ nguyên.

Story, Task, Sub-task và Issue dùng chung một list state của project. Plane bản này không cho mỗi loại work item một list status riêng.

Module là đối tượng khác, không phải work item. Plane có sẵn một list status cố định cho Module. Epic thành Module nên status của Epic được đổi sang list đó, không dùng list Backlog / Todo / In Progress của work item.

### Status của work item

| Status Jira | State Plane |
| --- | --- |
| Backlog, Idea | Backlog |
| To do, Pending | Todo |
| In Progress, Reopened, Fixing | In Progress |
| Testing, In Testing, Verifying | In Testing |
| Ready to Golive | Ready to Golive |
| Done | Done |
| Closed | Closed |
| Canceled | Cancelled |

### Status của Module khi Epic được tạo thành Module

| Status Jira của Epic | Status Module |
| --- | --- |
| Backlog, Idea | backlog |
| To do, Pending | planned |
| In Progress, Reopened, Fixing, Testing, In Testing, Verifying, Ready to Golive | in-progress |
| Done, Closed | completed |
| Canceled | cancelled |

### Priority

| Jira | Plane |
| --- | --- |
| Highest, Blocker | Urgent |
| High | High |
| Medium | Medium |
| Low | Low |
| Lowest | None |

Story point, ngày tạo, ngày sửa và reporter được ghi vào cuối mô tả. Ngày hạn Jira sang target date. Ngày bắt đầu Jira sang start date khi field đó có giá trị. Comment ghi kèm tên người viết trên Jira. Mỗi work item có link về ticket Jira.
