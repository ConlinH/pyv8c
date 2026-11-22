"""Chrome click listeners observe pre-activation, including label forwarding."""
import pytest


@pytest.mark.parametrize('through_label', [False, True])
@pytest.mark.parametrize('mouse', [False, True])
async def test_checkbox_listener_sees_new_state_and_changes_once(page, through_label, mouse):
    (await page.set_content('<body><label id="label" style="display:block;width:100px;height:24px">'
                     '<input id="control" type="checkbox" style="width:20px;height:20px">Label</label></body>', wait_until="commit"))
    page.evaluate('''window.observed=[];
        const control=document.getElementById('control');
        control.indeterminate=true;
        control.onclick=e=>observed.push([control.checked,control.indeterminate,e.isTrusted,
            e.constructor.name,e.pointerType,e.pointerId,e.isPrimary,e.pressure]);
        control.onchange=()=>observed.push('change');''')
    target = 'label' if through_label else 'control'
    if mouse:
        (await page.click('#' + target))
    else:
        page.evaluate(f'document.getElementById("{target}").click()')
    assert page.evaluate('observed') == [[True, False, mouse, 'PointerEvent',
                                       'mouse' if mouse else '', 1 if mouse else -1, False, 0], 'change']
    assert page.evaluate('[control.checked,control.indeterminate]') == [True, False]


async def test_cancelled_checkbox_click_restores_state_and_emits_no_change(page):
    (await page.set_content('<body><input id="control" type="checkbox"></body>', wait_until="commit"))
    result = page.evaluate('''(()=>{
        const control=document.getElementById('control');
        const observed=[];
        control.indeterminate=true;
        control.onclick=e=>{observed.push([control.checked,control.indeterminate]);e.preventDefault()};
        control.onchange=()=>observed.push('change');
        control.click();
        const restored=[control.checked,control.indeterminate];
        control.setAttribute('checked','');
        return [observed,restored,control.checked];
    })()''')
    assert result == [[[True, False]], [False, True], False]
